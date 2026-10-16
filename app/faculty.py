"""Stage 3: find faculty/staff directory pages and PDFs for a department."""
from urllib.parse import unquote, urlparse

from loguru import logger
from pydantic import BaseModel

from . import config, llm
from .http import fetch, fetch_rendered
from .util import extract_links, host, in_site, registered_domain, root_url, tokens

# A page qualifies only through STRONG words; WEAK words just rank pages that already qualify.
STRONG = {"faculty", "faculties", "people", "staff", "directory", "hod", "members", "member", "personnel"}
WEAK = {"head": 0.5, "chair": 0.5, "chairperson": 1.0, "professor": 0.5, "professors": 0.5,
        "team": 0.5, "academic": 0.3, "leadership": 0.5, "administration": 0.3, "teaching": 0.3}
# Pages about awards, alumni, news, accreditation, hiring... mention faculty/members but are not the
# department's roster
EXCLUDE = {
    "recognitions", "recognition", "awards", "award", "alumni", "news", "events", "event", "seminar",
    "seminars", "students", "student", "scholars", "phd", "emeritus", "retired", "former", "honorary",
    "adjunct", "visiting", "distinguished", "convocation", "highlights", "research", "publications",
    "projects", "courses", "admissions", "admission",
    # hiring, accreditation and committee documents ("Faculty Eligibility", "IQAC Members" ...)
    "eligibility", "iqac", "naac", "nirf", "aqar", "ssr", "committee", "committees", "careers",
    "career", "recruitment", "vacancy", "vacancies", "advertisement", "mandatory", "disclosure",
    "policy", "policies", "calendar", "calendars", "brochure", "fee", "fees", "scholarship",
    "scholarships",
}
COMMON_PATHS = ["/faculty/", "/faculty", "/people/", "/people/faculty", "/faculty.php", "/faculty-members",
                "/people/faculty-members", "/our-people", "/staff", "/directory"]
_SKIP_EXT = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".css", ".js", ".zip", ".doc", ".docx",
             ".ppt", ".pptx", ".xls", ".xlsx", ".mp4", ".mp3", ".ico")


class _Picks(BaseModel):
    indexes: list[int] = []


def score_page(url: str, text: str = "") -> float:
    path = (unquote(urlparse(url).path).replace("-", " ").replace("_", " ")
            .replace("/", " ").replace(".", " "))
    path_toks = set(tokens(path))
    text_toks = set(tokens(text))
    toks = path_toks | text_toks

    # If the anchor text explicitly has STRONG faculty words (e.g. 'Faculty Members', 'Faculty', 'People'),
    # do not allow path-only generic CMS terms like 'courses' or 'programs' to exclude it.
    strong_in_text = bool(text_toks & STRONG)
    if strong_in_text:
        text_exclude = EXCLUDE - {"courses", "projects", "phd"}
        if text_toks & text_exclude:
            return 0.0
    else:
        if toks & EXCLUDE:
            return 0.0

    s = 3.0 * min(2, len(toks & STRONG))
    if "faculty" in text_toks:
        s += 2.0
    if "head" in toks and toks & {"department", "dept"}:
        s += 3.0
    if toks & {"administrative", "nonteaching"} or ("office" in toks and "staff" in toks):
        s -= 2.0
    if s <= 0:
        return 0.0
    return s + sum(w for t, w in WEAK.items() if t in toks)


def _links(html: str, base_url: str, reg: str) -> list[tuple[str, str]]:
    return [(u, t) for u, t in extract_links(html, base_url) if in_site(u, reg)]


def _llm_pages(links: list[tuple[str, str]]) -> list[str]:
    uniq = list(dict.fromkeys(links))[:25]
    if not uniq or not llm.available():
        return []
    lines = "\n".join(f"{i}. {u} | {t}" for i, (u, t) in enumerate(uniq))
    res = llm.ask(
        "Which of these links lead to a faculty/staff directory or the head of department page? "
        f"Return up to 3 indexes.\n{lines}", _Picks)
    if not res:
        return []
    return [uniq[i][0] for i in res.indexes if 0 <= i < len(uniq)][:3]


def find_faculty_pages(dept_url: str, sitemap_urls: list[str]) -> list[str]:
    reg = registered_domain(dept_url)
    dh = host(dept_url)
    dpath = urlparse(dept_url).path
    prefix = dpath.rstrip("/")
    cands: dict[str, float] = {}

    def add(url: str, text: str):
        if url.lower().split("?")[0].endswith(_SKIP_EXT):
            return
        s = score_page(url, text)
        if s <= 0:
            return
        if host(url) == dh:
            s += 1
        if url.lower().split("?")[0].endswith(".pdf"):
            s += 0.5
        cands[url] = max(cands.get(url, 0.0), s)

    links: list[tuple[str, str]] = []
    page = fetch(dept_url)
    if page and page.ok and not page.is_pdf:
        links = _links(page.text, page.url, reg)
    for u, t in links:
        add(u, t)
    for u in sitemap_urls:
        if host(u) == dh and (not prefix or urlparse(u).path.startswith(prefix)):
            add(u, "")

    if not cands:  # menus built by JavaScript only exist after rendering
        rendered = fetch_rendered(dept_url)
        if rendered:
            links = _links(rendered.text, rendered.url, reg)
            for u, t in links:
                add(u, t)

    own_site = len([s for s in dpath.split("/") if s]) <= 1  # department has its own host/root
    if not cands and own_site:  # common faculty paths on the department's own site
        base = root_url(dept_url)
        for path in COMMON_PATHS:
            p = fetch(base + path, timeout=6, retries=False)
            if (p and p.ok and in_site(p.url, reg) and urlparse(p.url).path.strip("/")
                    and (p.is_pdf or len(p.text) > 500)):
                cands[p.url] = 2.0
                break

    if not cands:  # institution-wide people/directory subdomains
        for sub in ("people", "directory"):
            u = f"https://{sub}.{reg}/"
            p = fetch(u, timeout=6, retries=False)
            if p and p.ok:
                cands[u] = 1.0

    if not cands:
        for u in _llm_pages(links):
            cands[u] = 1.0

    ordered = [u for u, _ in sorted(cands.items(), key=lambda kv: -kv[1])]
    ordered = [u for u in ordered if u != dept_url][: config.MAX_FACULTY_PAGES - 1]
    ordered.append(dept_url)  # the department page itself often names the head
    logger.info(f"[faculty] {len(ordered)} candidate pages: {ordered[:3]}")
    return ordered