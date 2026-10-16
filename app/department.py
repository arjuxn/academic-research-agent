"""Stage 2: find and validate the academic department page."""
import gzip
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup
from loguru import logger
from pydantic import BaseModel
from rapidfuzz import fuzz

from . import config, llm
from .http import crawl_js_navigation, fetch, fetch_robust, looks_blocked
from .util import (
    clean,
    extract_links,
    host,
    in_site,
    norm,
    registered_domain,
    root_url,
    tokens,
)


@dataclass
class Dept:
    name: str
    url: str
    confidence: float
    via: str
    evidence_text: str = ""
    candidates: list = field(default_factory=list)


_SKIP_EXT = (
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".css", ".js", ".zip",
    ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".mp4",
    ".mp3", ".pdf", ".ico", ".xml",
)
DEPT_WORDS = {
    "department", "departments", "dept", "school", "schools", "division"
}
PROGRAM_WORDS = {
    "btech", "mtech", "bsc", "msc", "phd", "bachelor", "bachelors",
    "master", "masters", "mba", "admission", "admissions", "syllabus",
    "curriculum", "program", "programme", "programs", "programmes",
    "course", "courses", "degree", "undergraduate", "postgraduate",
    "scholarship", "news", "events", "event", "placement", "placements",
    "alumni", "lab", "labs", "ug", "pg", "ft", "pt", "cands",
    "candidate", "candidates", "rank", "ranklist", "counselling",
    "counseling", "allotment", "eligibility", "fee", "fees", "intake",
}
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
HUB_RE = re.compile(
    r"department|school|faculties|academics?|programmes?|programs|colleges?",
    re.I,
)
_sitemaps: dict[str, list[str]] = {}


# ---------- Sitemap discovery ----------

def _read_sitemap(url: str) -> str | None:
    page = fetch(url)
    if not page or not page.ok:
        return None

    data = page.content
    if url.endswith(".gz") or data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except Exception:
            return None

    return data.decode("utf-8", errors="replace")


def get_sitemap_urls(base: str) -> list[str]:
    if base in _sitemaps:
        return _sitemaps[base]

    queue = [base + "/sitemap.xml", base + "/sitemap_index.xml"]
    robots = fetch(base + "/robots.txt")
    if robots and robots.ok:
        queue = re.findall(r"(?im)^sitemap:\s*(\S+)", robots.text) + queue

    urls, seen, fetched = [], set(), 0
    queue = list(dict.fromkeys(queue))

    while queue and fetched < 15 and len(urls) < config.MAX_SITEMAP_URLS:
        sitemap = queue.pop(0)
        if sitemap in seen:
            continue

        seen.add(sitemap)
        fetched += 1
        text = _read_sitemap(sitemap)
        if not text:
            continue

        soup = BeautifulSoup(text, "xml")

        for sitemap_tag in soup.find_all("sitemap"):
            loc = sitemap_tag.find("loc")
            if loc:
                queue.append(loc.get_text(strip=True))

        for url_tag in soup.find_all("url"):
            loc = url_tag.find("loc")
            if loc:
                urls.append(loc.get_text(strip=True))

    _sitemaps[base] = urls[:config.MAX_SITEMAP_URLS]
    return _sitemaps[base]


# ---------- Navigation discovery ----------

def nav_links(base: str) -> list[tuple[str, str]]:
    reg = registered_domain(base)
    home = fetch_robust(base)

    if not home or not home.ok or home.is_pdf:
        return []

    links = [
        (url, title)
        for url, title in extract_links(home.text, home.url)
        if in_site(url, reg)
    ]
    output = list(links)

    hubs = [
        url for url, title in links
        if HUB_RE.search(f"{title} {urlparse(url).path}")
    ]

    for hub in list(dict.fromkeys(hubs))[:4]:
        page = fetch_robust(hub)
        if page and page.ok and not page.is_pdf:
            output.extend(
                (url, title)
                for url, title in extract_links(page.text, page.url)
                if in_site(url, reg)
            )

    # When static links produce no hubs or very few links, interact with JS menus
    if not hubs or len(links) < 25:
        js_links = [
            (url, title)
            for url, title in crawl_js_navigation(base)
            if in_site(url, reg)
        ]
        output.extend(js_links)

    return output


# ---------- Discipline matching and scoring ----------

def discipline_terms(discipline: str) -> set[str]:
    normalized = norm(discipline)
    terms = {normalized}

    for key, aliases in config.DISCIPLINE_ALIASES.items():
        if fuzz.ratio(normalized, key) >= 85 or key in normalized:
            terms |= aliases | {key}

    return terms


def _depth(url: str) -> int:
    return len([part for part in urlparse(url).path.split("/") if part])


def _hay(url: str, text: str = ""):
    parsed = urlparse(url)
    host_tokens = set(tokens(parsed.netloc.replace(".", " ")))
    path_tokens = tokens(
        unquote(parsed.path)
        .replace("-", " ")
        .replace("_", " ")
        .replace("/", " ")
    )
    normalized_text = norm(text)
    haystack = " ".join(
        list(host_tokens) + path_tokens + ([normalized_text] if normalized_text else [])
    )
    return host_tokens, haystack, set(haystack.split())


def _base(terms: set[str], haystack: str, hay_tokens: set[str]) -> float:
    best = 0.0

    for term in terms:
        score = (
            fuzz.token_set_ratio(term, haystack)
            if " " in term
            else (100.0 if term in hay_tokens else 0.0)
        )
        best = max(best, score)

    return best


def term_match(terms: set[str], url: str, text: str = "") -> bool:
    """Gate 1: the discipline or a strong alias must match."""
    _, haystack, hay_tokens = _hay(url, text)
    return _base(terms, haystack, hay_tokens) >= 90


def _location_match(location: str, url: str, text: str = "") -> bool:
    """Require location evidence on the candidate URL or its own page."""
    wanted = set(tokens(location))
    if not wanted:
        return True

    evidence = set(tokens(f"{url} {text}"))
    return wanted.issubset(evidence)


def _host_has_term(terms: set[str], url: str) -> bool:
    return term_match(terms, root_url(url), "")


def _host_label_has_term(terms: set[str], url: str) -> bool:
    """Detect discipline-specific subdomains such as cse.example.edu."""
    hostname = host(url)
    reg = registered_domain(url)
    subdomain = (
        hostname[:-(len(reg) + 1)]
        if reg and hostname.endswith(reg)
        else hostname
    )
    labels = [label for label in subdomain.split(".") if label]

    for term in terms:
        if " " in term:
            continue
        for label in labels:
            if label == term or (
                len(term) >= 3
                and (label.endswith(term) or label.startswith(term))
            ):
                return True

    return False


def looks_like_dept_home(html: str, url: str, terms: set[str]) -> bool:
    """Reject program/admissions pages; prefer department homes with people links."""
    soup = BeautifulSoup(html, "lxml")
    title = clean(soup.title.get_text()) if soup.title else ""
    h1 = clean(soup.h1.get_text(" ")) if soup.h1 else ""

    if set(tokens(f"{title} {h1}")) & PROGRAM_WORDS:
        return False

    if _YEAR_RE.search(urlparse(url).path):
        return False

    link_tokens: set[str] = set()
    for link_url, link_title in extract_links(html, url):
        link_tokens |= set(tokens(link_title))
        link_tokens |= set(
            tokens(
                unquote(urlparse(link_url).path)
                .replace("-", " ")
                .replace("_", " ")
                .replace("/", " ")
            )
        )

    if link_tokens & {"faculty", "people", "staff", "members"}:
        return True

    return _host_label_has_term(terms, url)


def score_candidate(
    terms: set[str],
    url: str,
    text: str = "",
    from_nav: bool = False,
    geo: str = "",
) -> float:
    host_tokens, haystack, hay_tokens = _hay(url, text)
    base = _base(terms, haystack, hay_tokens)

    if base == 0:
        return 0.0

    score = base

    if hay_tokens & DEPT_WORDS:
        score += 8
    if hay_tokens & PROGRAM_WORDS:
        score -= 25
    if _YEAR_RE.search(urlparse(url).path):
        score -= 25

    if any(
        (term in host_tokens)
        if " " not in term
        else set(term.split()).issubset(host_tokens)
        for term in terms
    ):
        score += 8

    score -= 3 * max(0, _depth(url) - 2)

    if from_nav:
        score += 4

    clean_t = norm(text)
    if any(clean_t == norm(term) for term in terms):
        score += 15

    path_lower = urlparse(url).path.lower()
    if any(p in path_lower for p in ("/ug-", "/ug/", "btech", "undergraduate")):
        score += 2

    if geo and norm(geo) in norm(f"{url} {text}"):
        score += 15

    return max(0.0, min(100.0, score))


_ROUTING_NOISE = {
    "site", "index", "php", "default", "home", "content", "component",
    "view", "main", "page", "pages", "en", "www",
}


_DEGREE_PREFIX_RE = re.compile(r"^(?:ug|pg|phd|btech|mtech|bsc|msc|undergraduate|postgraduate)[-_]", re.I)


def _same_unit(a: str, b: str) -> bool:
    """Avoid duplicate URL variants without merging distinct campus pages."""
    pa, pb = urlparse(a), urlparse(b)

    netloc_a = pa.netloc.lower().split(":")[0]
    if netloc_a.startswith("www."):
        netloc_a = netloc_a[4:]
    netloc_b = pb.netloc.lower().split(":")[0]
    if netloc_b.startswith("www."):
        netloc_b = netloc_b[4:]

    if netloc_a != netloc_b:
        return False

    x, y = pa.path.rstrip("/"), pb.path.rstrip("/")

    if x.startswith(y) or y.startswith(x):
        return True

    last_x = _DEGREE_PREFIX_RE.sub("", x.rsplit("/", 1)[-1])
    last_y = _DEGREE_PREFIX_RE.sub("", y.rsplit("/", 1)[-1])

    if len(last_x) >= 4 and last_x == last_y:
        return True

    raw_x = x.rsplit("/", 1)[-1]
    raw_y = y.rsplit("/", 1)[-1]
    if len(raw_x) < 4 or raw_x != raw_y:
        return False

    prefix_x = x.rsplit("/", 1)[0] if "/" in x else ""
    prefix_y = y.rsplit("/", 1)[0] if "/" in y else ""

    remaining_x = set(tokens(prefix_x)) - _ROUTING_NOISE - DEPT_WORDS
    remaining_y = set(tokens(prefix_y)) - _ROUTING_NOISE - DEPT_WORDS

    return not remaining_x and not remaining_y


def _units(scored):
    output, kept = [], []

    for candidate in scored:
        if not any(_same_unit(candidate[1], url) for url in kept):
            output.append(candidate)
            kept.append(candidate[1])

    return output


def _score_all(terms, candidates, geo: str = ""):
    scored = sorted(
        (
            (score_candidate(terms, url, text, nav, geo=geo), url, text)
            for url, (text, nav) in candidates.items()
            if term_match(terms, url, text)
        ),
        key=lambda item: (-item[0], _depth(item[1]), len(item[1])),
    )
    return [candidate for candidate in scored if candidate[0] >= 50]


# ---------- Subdomain probing ----------

def probe_subdomains(base: str, terms: set[str]) -> list[tuple[str, str]]:
    reg = registered_domain(base)
    labels = sorted({
        term for term in terms if " " not in term and 2 <= len(term) <= 12
    })[:5]
    multiword_terms = [term for term in terms if " " in term]
    output = []

    for label in labels:
        page = fetch(f"https://{label}.{reg}", timeout=6, retries=False)

        if not page or not page.ok or page.is_pdf or not in_site(page.url, reg):
            continue

        soup = BeautifulSoup(page.text, "lxml")
        title = clean(soup.title.get_text()) if soup.title else ""
        title_tokens = set(tokens(title))

        if title_tokens & DEPT_WORDS or any(
            set(term.split()).issubset(title_tokens)
            for term in multiword_terms
        ):
            output.append((page.url, title))

    return output


def _hop_to_dept_site(page, terms: set[str], reg: str) -> str | None:
    """Follow a link to a discipline-specific subdomain where appropriate."""
    page_host = host(page.url)

    for url, _ in extract_links(page.text, page.url):
        if not in_site(url, reg) or host(url) == page_host or _depth(url) > 1:
            continue

        if url.lower().split("?")[0].endswith(_SKIP_EXT):
            continue

        if not _host_has_term(terms, url):
            continue

        linked_page = fetch(root_url(url), timeout=8, retries=False)

        if linked_page and linked_page.ok and not linked_page.is_pdf:
            if in_site(linked_page.url, reg):
                return linked_page.url

    return None


# ---------- Local LLM disambiguation ----------

class _Pick(BaseModel):
    index: int


def _llm_pick(shortlist, discipline):
    if not shortlist or not llm.available():
        return None

    lines = "\n".join(
        f"{index}. {url} | {text}"
        for index, (_, url, text) in enumerate(shortlist)
    )
    prompt = (
        f"We need the academic DEPARTMENT home page for {discipline}. "
        "Do not select a degree program, course, news item, lab, admissions list, "
        "or service centre.\n"
        f"Candidates:\n{lines}\n"
        "Reply with the index of the best department page, or -1 if none is correct."
    )

    result = llm.ask(prompt, _Pick)

    if result and 0 <= result.index < len(shortlist):
        _, url, text = shortlist[result.index]
        return url, text, 0.7, "llm"

    return None


def _pick(scored, discipline, geo: str = ""):
    if not scored:
        return None

    if geo:
        geo_matches = [
            candidate for candidate in scored
            if candidate[0] >= 60
            and _location_match(geo, candidate[1], candidate[2])
        ]

        if geo_matches:
            top = geo_matches[0]
            rivals = [
                candidate for candidate in geo_matches[1:]
                if top[0] - candidate[0] < 8
                and not _same_unit(top[1], candidate[1])
            ]

            if not rivals:
                return top[1], top[2], top[0] / 100, "deterministic-geo"

            return None, None, 0.0, "ambiguous"

        return None

    top = scored[0]
    rivals = [
        candidate for candidate in scored[1:]
        if top[0] - candidate[0] < 8
        and not _same_unit(top[1], candidate[1])
    ]

    if rivals and top[0] >= 75 and rivals[0][0] >= 75:
        return None, None, 0.0, "ambiguous"

    if top[0] >= 85 and not rivals:
        return top[1], top[2], top[0] / 100, "deterministic"

    choice = _llm_pick(_units(scored)[:5], discipline)
    if choice:
        return choice

    if not llm.available() and top[0] >= 70:
        return top[1], top[2], top[0] / 100 * 0.85, "deterministic-weak"

    return None


# ---------- Cross-host discovery and department-page search ----------

def _related_site(url: str, reg: str) -> bool:
    """Allow discovery on a related institutional host; page validation remains mandatory."""
    ignored = {"www", "edu", "ac", "org", "com", "in", "co"}
    target_tokens = set(tokens(reg)) - ignored
    candidate_tokens = set(tokens(registered_domain(url))) - ignored
    return bool(target_tokens & candidate_tokens)


def _search_fallback(
    reg: str,
    discipline: str,
    terms: set[str],
    blocked_urls: list | None = None,
    geo: str = "",
) -> Dept | None:
    try:
        from ddgs import DDGS

        ddg = DDGS()
        queries = []

        # Search for the actual department page, not just the homepage.
        if geo:
            queries.extend([
                f'site:{reg} {geo} "{discipline}" department',
                f'site:{reg} {geo} "Department of {discipline}"',
                f'site:{reg} {geo} {discipline} faculty',
                f'{reg} {geo} "{discipline}" department',
                f'"{reg.replace(".", " ")}" {geo} "{discipline}" department',
            ])

        queries.extend([
            f'site:{reg} "Department of {discipline}"',
            f'site:{reg} "{discipline}" department faculty',
            f'site:{reg} {discipline} people',
            f'"{reg.replace(".", " ")}" "{discipline}" department',
        ])

        results = []
        seen_urls = set()

        for query in queries:
            for result in ddg.text(query, max_results=8) or []:
                url = result.get("href") or result.get("url") or ""
                if url and url not in seen_urls:
                    results.append(result)
                    seen_urls.add(url)

    except Exception as exc:
        logger.debug(f"dept search failed: {exc}")
        return None

    ranked = []

    for result in results:
        url = result.get("href") or result.get("url") or ""
        title = result.get("title", "")
        body = result.get("body", "") or result.get("snippet", "")
        evidence = clean(f"{title}\n{body}")

        if not url or url.lower().split("?")[0].endswith(_SKIP_EXT):
            continue

        # Search may discover another official host. The candidate page must still pass
        # discipline, location, and department-home checks in _finalize().
        if not (in_site(url, reg) or _related_site(url, reg)):
            continue

        if not term_match(terms, url, evidence):
            continue

        score = score_candidate(terms, url, evidence, geo=geo)
        if score >= 60:
            ranked.append((score, url, evidence))

    ranked.sort(key=lambda item: (-item[0], _depth(item[1])))

    for score, url, evidence in ranked[:10]:
        department = _finalize(
            url,
            evidence,
            score / 100 * 0.85,
            "search",
            terms,
            blocked_urls=blocked_urls,
            geo=geo,
        )
        if department:
            return department

    return None


def _finalize(
    url,
    text,
    conf,
    via,
    terms,
    blocked_urls: list | None = None,
    geo: str = "",
) -> Dept | None:
    page = fetch_robust(url)

    if not page or not page.ok or page.is_pdf:
        if page is None or (page and page.status in (401, 403, 406, 429, 500, 502, 503)):
            if blocked_urls is not None:
                blocked_urls.append(url)
        return None

    # Some anti-bot services return a challenge page with HTTP 200. Treat that
    # as blocked rather than parsing the challenge as if it were the department.
    if looks_blocked(page.text):
        logger.info(f"[dept] bot-protection challenge returned for {url}")
        if blocked_urls is not None:
            blocked_urls.append(url)
        return None

    soup = BeautifulSoup(page.text, "lxml")
    h1 = clean(soup.h1.get_text(" ")) if soup.h1 else ""
    title = clean(soup.title.get_text()).split("|")[0].strip() if soup.title else ""
    visible_text = clean(soup.get_text(" ", strip=True))

    if not term_match(terms, page.url, f"{title} {h1} {visible_text[:3000]}"):
        logger.debug(f"[dept] rejected {url}: page does not mention the discipline")
        return None

    if geo and not _location_match(
        geo, page.url, f"{title} {h1} {visible_text[:3000]}"
    ):
        logger.info(
            f"[dept] rejected {url}: requested location '{geo}' is not supported by this page"
        )
        return None

    final, department_url = page, page.url

    if _depth(page.url) >= 1 and not _host_has_term(terms, page.url):
        hop = _hop_to_dept_site(page, terms, registered_domain(page.url))

        if hop:
            hopped_page = fetch(hop)
            if hopped_page and hopped_page.ok and not hopped_page.is_pdf:
                logger.info(f"[dept] following link to department site: {hop}")
                final, department_url, via = hopped_page, hop, via + "+site"

    if geo:
        final_soup = BeautifulSoup(final.text, "lxml")
        final_h1 = clean(final_soup.h1.get_text(" ")) if final_soup.h1 else ""
        final_title = (
            clean(final_soup.title.get_text()) if final_soup.title else ""
        )
        final_text = clean(final_soup.get_text(" ", strip=True))

        if not _location_match(
            geo,
            department_url,
            f"{final_title} {final_h1} {final_text[:3000]}",
        ):
            logger.info(
                f"[dept] rejected {department_url}: linked page does not support location '{geo}'"
            )
            return None

    if not looks_like_dept_home(final.text, final.url, terms):
        logger.info(f"[dept] rejected {final.url}: does not look like a department home page")
        return None

    final_soup = BeautifulSoup(final.text, "lxml")
    final_h1 = clean(final_soup.h1.get_text(" ")) if final_soup.h1 else ""
    final_title = (
        clean(final_soup.title.get_text()).split("|")[0].strip()
        if final_soup.title else ""
    )
    name = final_h1 or final_title or clean(text)
    name = re.split(r"\s+[|–—]\s+", name)[0].strip()

    return Dept(
        name[:120],
        department_url,
        round(min(conf, 0.95), 2),
        via,
        evidence_text=visible_text[:1500],
    )


def find_department(
    base: str,
    discipline: str,
    geo: str = "",
    show_candidates: bool = False,
) -> Dept | None:
    reg = registered_domain(base)
    terms = discipline_terms(discipline)
    candidates: dict[str, tuple[str, bool]] = {}

    for url in get_sitemap_urls(base):
        if in_site(url, reg) and not url.lower().split("?")[0].endswith(_SKIP_EXT):
            candidates.setdefault(url, ("", False))

    for url, title in nav_links(base):
        if url.lower().split("?")[0].endswith(_SKIP_EXT):
            continue

        old = candidates.get(url)
        if old is None or (title and not old[0]):
            candidates[url] = (title, True)

    scored = _score_all(terms, candidates, geo=geo)

    if not scored or scored[0][0] < 85:
        for url, title in crawl_js_navigation(base):
            if in_site(url, reg) and not url.lower().split("?")[0].endswith(_SKIP_EXT):
                old = candidates.get(url)
                if old is None or (title and not old[0]):
                    candidates[url] = (title, True)
        scored = _score_all(terms, candidates, geo=geo)

    if not scored or scored[0][0] < 85:
        for url, title in probe_subdomains(base, terms):
            candidates[url] = (title, True)
        scored = _score_all(terms, candidates, geo=geo)

    if show_candidates and scored:
        from rich.console import Console
        from rich.table import Table

        table = Table(title=f"Department Candidates for {discipline} ({base})")
        table.add_column("Rank", justify="right")
        table.add_column("Score", justify="right")
        table.add_column("URL")
        table.add_column("Title")

        for index, (score, url, title) in enumerate(scored[:10], 1):
            table.add_row(str(index), f"{score:.1f}", url, title[:60])

        Console().print(table)

    picked = _pick(scored, discipline, geo=geo)

    if picked and picked[3] == "ambiguous":
        return Dept("Ambiguous Department", "", 0.0, "ambiguous", candidates=scored)

    blocked_urls: list[str] = []
    department = (
        _finalize(*picked, terms, blocked_urls=blocked_urls, geo=geo)
        if picked else None
    )

    if department is None:
        tried = {picked[0]} if picked and picked[0] else set()

        for score, url, title in _units(scored)[:5]:
            if url in tried or score < 85:
                continue

            department = _finalize(
                url,
                title,
                score / 100,
                "deterministic",
                terms,
                blocked_urls=blocked_urls,
                geo=geo,
            )
            if department:
                break

    if department is None:
        department = _search_fallback(
            reg,
            discipline,
            terms,
            blocked_urls=blocked_urls,
            geo=geo,
        )

    if department is None and blocked_urls:
        logger.info(f"[dept] candidate department page blocked: {blocked_urls[0]}")
        return Dept(
            "Department Page Blocked",
            blocked_urls[0],
            0.0,
            "blocked",
            candidates=scored,
        )

    if department:
        department.candidates = scored
        logger.info(
            f"[dept] {department.name} -> {department.url} "
            f"({department.via}, {department.confidence})"
        )

    return department