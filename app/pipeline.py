
"""One university + discipline -> one Result row. Shared by all three modes."""
import re

from loguru import logger

from rapidfuzz import fuzz

from . import config
from .confidence import score
from .contacts import (
    best_contact,
    domain_ok,
    extract_html,
    extract_pdf,
    finalize,
    find_emails,
    find_phone,
    is_generic,
    llm_contacts,
)
from .debug import DebugTrace
from .department import find_department, get_sitemap_urls
from .faculty import find_faculty_pages
from .http import fetch, fetch_rendered
from .models import Result
from .util import norm, registered_domain, root_url
from .website import discover_websites


def _looks_js(html: str) -> bool:
    visible = re.sub(
        r"<script.*?</script>|<style.*?</style>|<[^>]+>",
        " ",
        html,
        flags=re.S,
    )
    return "<script" in html and len(visible.strip()) < 1500


def _extract(url: str, reg: str, trace: DebugTrace | None = None):
    page = fetch(url)
    if not page or not page.ok:
        if trace:
            trace.add("contact_page", "fetch_failed", url=url)
        return [], ""

    if page.is_pdf:
        contacts, text = extract_pdf(page.content, page.url, reg)
        if trace:
            trace.add(
                "contact_page",
                "extracted",
                url=page.url,
                kind="pdf",
                contacts=len(contacts),
                text_chars=len(text),
            )
        return contacts, text

    contacts, text = extract_html(page.text, page.url, reg)

    if not contacts and config.USE_PLAYWRIGHT and _looks_js(page.text):
        rendered = fetch_rendered(page.url)
        if rendered:
            contacts, text = extract_html(rendered.text, rendered.url, reg)
            if trace:
                trace.add(
                    "contact_page",
                    "rendered_extracted",
                    url=rendered.url,
                    kind="html",
                    contacts=len(contacts),
                    text_chars=len(text),
                )
            return contacts, text

    if trace:
        trace.add(
            "contact_page",
            "extracted",
            url=page.url,
            kind="html",
            contacts=len(contacts),
            text_chars=len(text),
        )
    return contacts, text


def _department_for_site(
    site: str,
    res: Result,
    trace: DebugTrace,
    show_candidates: bool,
):
    """Try to resolve a usable department on one website candidate."""
    dept = find_department(
        site,
        res.discipline,
        geo=res.geo,
        show_candidates=show_candidates,
    )

    if dept and dept.via == "ambiguous":
        trace.add(
            "department",
            "ambiguous",
            site=site,
            candidates=[
                {"score": s, "url": u, "title": t}
                for s, u, t in (dept.candidates or [])
            ],
        )
        return None, "Ambiguous Department", dept.url, (
            "Stage department: multiple department candidates found across "
            "campuses without disambiguating geo"
        )

    if dept and dept.via == "blocked":
        trace.add(
            "department",
            "blocked",
            site=site,
            url=dept.url,
            candidates=[
                {"score": s, "url": u, "title": t}
                for s, u, t in (dept.candidates or [])
            ],
        )
        return None, "Department Page Blocked", dept.url, (
            f"Stage department: candidate department page ({dept.url}) "
            "was blocked or unreachable"
        )

    if not dept or dept.confidence < config.DEPT_MIN_CONFIDENCE:
        if dept:
            logger.info(
                f"[dept] rejected {dept.url}: confidence {dept.confidence} "
                f"below {config.DEPT_MIN_CONFIDENCE}"
            )
            trace.add(
                "department",
                "rejected_low_confidence",
                site=site,
                url=dept.url,
                confidence=dept.confidence,
                minimum=config.DEPT_MIN_CONFIDENCE,
                via=dept.via,
            )
            err = (
                "Stage department: best candidate below confidence threshold "
                f"({dept.confidence} < {config.DEPT_MIN_CONFIDENCE})"
            )
            durl = dept.url
        else:
            trace.add(
                "department",
                "not_found",
                site=site,
                discipline=res.discipline,
            )
            err = (
                "Stage department: no page looked like the requested "
                "discipline department"
            )
            durl = None

        return None, "Field Not Found", durl, err

    return dept, None, None, None


def _failure_priority(status: str | None) -> int:
    """Rank failure outcomes so useful evidence isn't hidden by an earlier failure."""
    priorities = {
        "Website Not Found": 0,
        "Field Not Found": 1,
        "Ambiguous Department": 2,
        "Department Page Blocked": 3,
    }
    return priorities.get(status or "", 0)


def _run(
    res: Result,
    website: str,
    trace: DebugTrace,
    show_candidates: bool = False,
) -> Result:
    if website.strip():
        site = website.strip()
        site = root_url(site if site.startswith("http") else "https://" + site)
        trace.add("website", "provided", url=site)
        site_candidates = [site]
    else:
        # Campus and discipline hints help discover the right constituent college.
        cands = discover_websites(
            res.university_name,
            res.country,
            geo=res.geo,
            discipline=res.discipline,
        )
        trace.add(
            "website",
            "candidates",
            candidates=[c.to_dict() for c in cands],
        )

        if show_candidates and cands:
            from rich.console import Console
            from rich.table import Table

            table = Table(title=f"Website Candidates for {res.university_name}")
            table.add_column("Rank", justify="right")
            table.add_column("Score", justify="right")
            table.add_column("URL")
            table.add_column("Source")
            table.add_column("Reason")

            for i, candidate in enumerate(cands, 1):
                table.add_row(
                    str(i),
                    f"{candidate.score:.2f}",
                    candidate.url,
                    candidate.source,
                    candidate.reason,
                )
            Console().print(table)

        site_candidates = [
            candidate.url
            for candidate in cands[:config.MAX_WEBSITE_CANDIDATES]
        ]

    if not site_candidates:
        res.status = "Website Not Found"
        res.error = (
            "Stage website: no official website candidate passed verification"
        )
        trace.add("website", "not_found")
        return res

    # Try ranked candidates until one yields a usable department page.
    # Keep the most informative failure if every candidate fails.
    dept = None
    chosen_site = None
    fallback = None

    for i, site in enumerate(site_candidates):
        trace.add(
            "website",
            "selected",
            url=site,
            source="input" if website.strip() else "resolver",
            attempt=i + 1,
            of=len(site_candidates),
        )

        dept, status, durl, err = _department_for_site(
            site,
            res,
            trace,
            show_candidates,
        )

        if dept:
            chosen_site = site
            break

        current_failure = (status, durl, err, site)

        if (
            fallback is None
            or _failure_priority(status) > _failure_priority(fallback[0])
        ):
            fallback = current_failure

        if i + 1 < len(site_candidates):
            logger.info(
                f"[website] {site} did not yield a usable department page "
                f"({status}); trying next candidate"
            )

    if dept is None:
        status, durl, err, site = fallback
        res.college_website = site
        res.status = status
        if durl:
            res.department_url = durl
        res.error = err

        trace.add(
            "department",
            "all_candidates_exhausted",
            tried=site_candidates,
            selected_failure=status,
            selected_site=site,
            selected_department_url=durl,
        )
        return res

    res.college_website = chosen_site
    res.teaching_requested_field = "YES"
    res.target_department = dept.name
    res.department_url = dept.url

    trace.add(
        "department",
        "selected",
        name=dept.name,
        url=dept.url,
        confidence=dept.confidence,
        via=dept.via,
    )

    reg = registered_domain(chosen_site)
    pages = find_faculty_pages(dept.url, get_sitemap_urls(chosen_site))
    trace.add("faculty_pages", "selected", pages=pages)

    contacts, texts, best = [], [], None

    for url in pages:
        found, text = _extract(url, reg, trace)
        contacts = finalize(contacts + found, reg)

        if text:
            texts.append((url, text))

        best = best_contact(contacts)
        if best and best.rank <= 1 and best.email:
            break

    # Follow individual profile or bio links (e.g. bio PDFs or profile sub-pages) for top contacts
    if best and not best.email:
        for c in sorted([c for c in contacts if c.profile_url and not c.email and c.rank <= 2], key=lambda x: x.rank)[:3]:
            logger.info(f"[contact] Following profile/bio link for {c.name}: {c.profile_url}")
            prof_contacts, prof_text = _extract(c.profile_url, reg, trace)
            for pc in prof_contacts:
                if fuzz.ratio(norm(pc.name), norm(c.name)) >= 60 or not pc.name:
                    if pc.email and not c.email:
                        c.email = pc.email
                    if pc.phone and not c.phone:
                        c.phone = pc.phone
                    if pc.office and not c.office:
                        c.office = pc.office
                    if pc.research_area and not c.research_area:
                        c.research_area = pc.research_area
            if not c.email and prof_text:
                emails = [e for e in find_emails(prof_text) if domain_ok(e, reg) and not is_generic(e)]
                if emails:
                    c.email = emails[0]
                if not c.phone:
                    c.phone = find_phone(prof_text)
            contacts = finalize(contacts, reg)
            best = best_contact(contacts)
            if best and best.rank <= 1 and best.email:
                break

    if best is None:
        trace.add(
            "llm_fallback",
            "attempted",
            pages=[url for url, _ in texts[:2]],
        )
        contacts = finalize(contacts + llm_contacts(texts, reg), reg)
        best = best_contact(contacts)

    if not best:
        res.status = "Contact Not Found"
        res.error = (
            "Stage contact: no validated faculty/head/chair contact found"
        )
        trace.add(
            "contact",
            "not_found",
            contacts_seen=len(contacts),
            pages=[url for url, _ in texts],
        )
        return res

    logger.info(
        f"[contact] {best.name} | {best.role} | "
        f"{best.method} | {best.source_url}"
    )

    trace.add(
        "contact",
        "selected",
        name=best.name,
        role=best.role,
        email=best.email,
        method=best.method,
        source_url=best.source_url,
        profile_url=best.profile_url,
    )

    res.contact_type = best.role
    res.contact_name = best.name
    res.email = best.email
    res.phone = best.phone
    res.office = best.office
    res.profile_url = best.profile_url
    res.source_url = best.source_url
    res.research_area = best.research_area
    res.extraction_method = best.method

    for key, value in score(best, dept.url, dept.confidence).items():
        setattr(res, key, value)

    res.status = "Success"
    return res


def run_university(
    university: str,
    discipline: str,
    geo: str = "",
    country: str = "",
    website: str = "",
    campus: str = "",
    show_candidates: bool = False,
) -> Result:
    # Combine location hints without duplicating values such as Pune + Pune.
    location_parts: list[str] = []

    for value in (campus, geo):
        value = value.strip()
        if not value:
            continue

        value_norm = " ".join(value.lower().split())
        replaced = False

        for i, existing in enumerate(location_parts):
            existing_norm = " ".join(existing.lower().split())

            if value_norm == existing_norm or value_norm in existing_norm:
                replaced = True
                break

            if existing_norm in value_norm:
                location_parts[i] = value
                replaced = True
                break

        if not replaced:
            location_parts.append(value)

    effective_geo = " ".join(location_parts)

    res = Result(
        university_name=university,
        geo=effective_geo,
        country=country,
        discipline=discipline,
    )
    trace = DebugTrace(
        university=university,
        discipline=discipline,
        geo=effective_geo,
        country=country,
    )

    try:
        res = _run(res, website, trace, show_candidates=show_candidates)
    except Exception as e:
        logger.exception(f"pipeline error for {university}")
        res.status, res.error = "Error", str(e)
        trace.add("pipeline", "error", error=str(e))

    try:
        res.debug_file = str(trace.write())
    except Exception as e:
        logger.warning(f"debug trace write failed for {university}: {e}")

    logger.info(
        f"[done] {university} | {discipline} -> "
        f"{res.status} {res.contact_name}"
    )
    return res
