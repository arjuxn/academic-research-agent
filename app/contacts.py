"""Stage 4: contact extraction. Tables > structured HTML > deterministic text > Qwen (validated)."""
import re
from urllib.parse import unquote, urljoin

from bs4 import BeautifulSoup
from loguru import logger
from pydantic import BaseModel
from rapidfuzz import fuzz

from . import config, llm
from .models import Contact
from .pdfs import pdf_tables, pdf_text
from .util import clean, norm

METHOD_ORDER = {"HTML_TABLE": 0, "PDF_TABLE": 0, "STRUCTURED_HTML": 1, "DETERMINISTIC": 2, "LLM": 3}

# ---------------- emails / phones ----------------
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
_AT = re.compile(r"\s*[\[\(\{]\s*at\s*[\]\)\}]\s*", re.I)
_DOT = re.compile(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", re.I)
PHONE_RE = re.compile(r"(?<![\w@])\+?\(?\d[\d\s\-().]{6,}\d(?![\w@])")
_BAD_TLD = {"png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js"}


def deobfuscate(text: str) -> str:
    return _DOT.sub(".", _AT.sub("@", text))


def find_emails(text: str) -> list[str]:
    out = []
    for e in EMAIL_RE.findall(deobfuscate(text)):
        e = e.lower()
        if e.rsplit(".", 1)[-1] not in _BAD_TLD:
            out.append(e)
    return out


def find_phone(text: str) -> str:
    m_labeled = re.search(r"(?i)\b(?:phone|tel|mobile|cell|office\s+contact|contact\s+no|contact)\b\s*[:\-–]?\s*(\+?\(?\d[\d\s\-().]{6,}\d)", text)
    if m_labeled:
        s = m_labeled.group(1).strip()
        digits = re.sub(r"\D", "", s)
        if 8 <= len(digits) <= 15:
            return s

    for m in PHONE_RE.finditer(EMAIL_RE.sub(" ", text)):
        s = m.group().strip()
        digits = re.sub(r"\D", "", s)
        if not 8 <= len(digits) <= 15:
            continue
        if re.fullmatch(r"(?:19|20)\d{2}\s*[-–]\s*(?:19|20)\d{2}", s):
            continue
        m_year = re.match(r"^(?:19|20)\d{2}\s+(\d{8,12})$", s)
        if m_year:
            return m_year.group(1)
        return s
    return ""


def domain_ok(email: str, reg: str) -> bool:
    dom = email.split("@")[-1]
    return dom == reg or dom.endswith("." + reg)


def is_generic(email: str) -> bool:
    local = email.split("@")[0].lower()
    toks = [t for t in re.split(r"[._\-+\d]+", local) if t]
    return any(t in config.GENERIC_MAILBOX_WORDS for t in toks)


# ---------------- roles ----------------
HOD_RE = re.compile(
    r"\bhead\s+of\s+(?:the\s+)?(?:dept|department|school|division|discipline|section)\b"
    r"|\bhod\b|\bdepartment(?:al)?\s+head\b|\bdept\.?\s+head\b"
    r"|\bhead\s*[,&/\-]\s*(?:dept|department)"
    r"|\bhead\s*(?:,|&|and|/|-)\s*prof"
    r"|\b(?:prof\w*|dr)\.?\s*(?:,|&|and|/|-)\s*head\b"
    r"|\(\s*head\s*\)|^\s*head\s*$",
    re.I,
)
# "Associate Head of the Department", "Deputy Head" ... are NOT the head
SUBHEAD_RE = re.compile(
    r"\b(?:associate|assistant|deputy|vice|joint|co)[\s\-]*head"
    r"(?:\s+of\s+(?:the\s+)?(?:dept|department|school|division))?\b", re.I)
CHAIR_RE = re.compile(r"\bchair(?:person|man|woman)?\b", re.I)
# Endowed titles ("Distinguished Chair Professor", "Institute Chair Professor") are not department chairs
ENDOWED_CHAIR_RE = re.compile(
    r"\bchair(?:ed)?\s+prof|\b(?:distinguished|endowed|named|institute|visiting|emeritus)\s+chair\b", re.I)
ASSIST_RE = re.compile(r"\b(?:assistant|asst\.?)\s+prof(?:essor)?\b", re.I)
ASSOC_RE = re.compile(r"\b(?:associate|assoc\.?)\s+prof(?:essor)?\b", re.I)
PROF_FULL_RE = re.compile(r"\bprofessor\b", re.I)
PROF_ABBR_RE = re.compile(r"\bprof\b\.?", re.I)
OTHER_RE = re.compile(r"\b(?:lecturer|reader|instructor|scientist)\b", re.I)
EXCLUDE_RE = re.compile(r"\b(?:former|past|previous|late|retired|emerit\w*)\b", re.I)
ADJ_RE = re.compile(r"\b(?:adjunct|visiting|honorary|courtesy)\b", re.I)
ROLE_ANY = re.compile(
    "|".join(f"(?:{p.pattern})" for p in
             (HOD_RE, CHAIR_RE, ASSIST_RE, ASSOC_RE, PROF_FULL_RE, PROF_ABBR_RE, OTHER_RE)), re.I)
# News / alumni / event wording: a line like this mentions a person, it is not a roster entry
NEWS_CUE_RE = re.compile(
    r"\b(?:alumni|alumnus|alumna|award|awards|awarded|lecture|lectures|talk|keynote|seminar|webinar|"
    r"colloquium|honou?rs?|congratulations|felicitat\w*|speaker|guest|invited|convocation|reunion|"
    r"memoriam|obituary|passed\s+away)\b", re.I)


def detect_role(text: str) -> tuple[str, int] | None:
    """Return (label, rank) or None. rank: 0 HOD, 1 Chair, 2 Prof, 3 Assoc, 4 Asst, 5 Other."""
    t = SUBHEAD_RE.sub(" ", clean(text))
    if not t.strip() or EXCLUDE_RE.search(t):
        return None
    if HOD_RE.search(t):
        return ("Head of Department", 0)
    if CHAIR_RE.search(t) and not ENDOWED_CHAIR_RE.search(t):
        return ("Chair", 1)
    if ADJ_RE.search(t) and (PROF_FULL_RE.search(t) or ASSOC_RE.search(t) or ASSIST_RE.search(t)):
        return ("Other Academic", 5)
    if ASSIST_RE.search(t):
        return ("Assistant Professor", 4)
    if ASSOC_RE.search(t):
        return ("Associate Professor", 3)
    if PROF_FULL_RE.search(t):
        return ("Professor", 2)
    if OTHER_RE.search(t):
        return ("Other Academic", 5)
    if PROF_ABBR_RE.search(t):
        return ("Professor", 2)
    return None


# ---------------- names ----------------
TITLE_RE = re.compile(
    r"^(?:(?:dr|prof|professor|mr|mrs|ms|miss|shri|smt|sri|er|engr|sir|madam)\b\.?\s*)+", re.I)
TITLE_CUE_RE = re.compile(r"\b(?:Dr|Mr|Mrs|Ms|Shri|Smt|Sri)\b\.?\s+[A-Z]|\bProf(?:essor)?\b\.?\s+[A-Z][a-z]")
PARTICLES = {"van", "von", "de", "der", "den", "bin", "al", "el", "da", "di", "la", "le", "du", "ibn", "ben"}
NAME_BAD = {
    # structure / page words
    "department", "dept", "university", "school", "college", "institute", "faculty", "staff", "email",
    "mail", "phone", "contact", "office", "research", "engineering", "science", "sciences",
    "technology", "studies", "centre", "center", "head", "chair", "professor", "lecturer", "director",
    "dean", "address", "tel", "fax", "home", "about", "news", "events", "profile", "view", "more",
    "read", "click", "here", "publications", "interests", "building", "room", "floor", "campus",
    "welcome", "message", "page", "list", "members", "directory", "people", "our", "the", "of", "and",
    "for", "in", "at", "name", "designation", "position", "title", "role", "sl", "no", "s",
    "associate", "assistant", "deputy", "vice", "joint",
    # organisations / awards / events: things that look like names but are not people
    "award", "awards", "alumni", "alumnus", "distinguished", "service", "services", "committee",
    "society", "association", "lab", "laboratory", "group", "club", "cell", "board", "council",
    "prize", "fellow", "fellowship", "medal", "lecture", "lectures", "conference", "symposium",
    "seminar", "workshop", "program", "programme", "project", "projects", "academy", "foundation",
    "trust", "annual", "national", "international", "global", "excellence", "scholarship", "scholar",
    "student", "students", "batch", "class", "year", "convocation", "ceremony", "festival",
    "orientation", "admission", "admissions", "placement", "placements", "recruitment", "vacancy",
    "notice", "notification", "circular", "tender", "result", "results", "schedule", "timetable",
    "syllabus", "curriculum", "examination", "exam", "exams", "computer", "computing", "software",
    "hardware", "data", "artificial", "intelligence", "machine", "learning", "network", "networks",
    "systems", "security", "design", "management", "business", "applied", "advanced", "general",
    "public", "private", "online", "digital", "life", "library", "hostel", "sports", "cultural",
}


def clean_name(s: str) -> str:
    s = clean(s)
    s = re.sub(r"\([^)]*\)", " ", s)
    s = SUBHEAD_RE.sub(" ", s)
    s = ROLE_ANY.sub(" ", s)
    s = TITLE_RE.sub("", clean(s)).strip(" ,.;:-–—|/")
    s = re.split(r"[,;|/]| [-–—] ", s)[0]
    return clean(s)


def valid_name(s: str) -> bool:
    toks = s.split()
    if not 2 <= len(toks) <= 5:
        return False
    letters = 0
    for t in toks:
        tl = t.lower().strip(".,")
        if tl in PARTICLES:
            continue
        if tl in NAME_BAD or not t[0].isupper():
            return False
        if not all(ch.isalpha() or ch in "'’-." for ch in t):
            return False
        letters += sum(ch.isalpha() for ch in t)
    return letters >= 4


def has_title(line: str) -> bool:
    """Dr./Prof./Shri... directly before a capitalised word: evidence the phrase is a person."""
    return bool(TITLE_CUE_RE.search(line))


def email_matches_name(email: str, name: str) -> float:
    local = re.sub(r"[^a-z]", "", email.split("@")[0].lower())
    parts = [p for p in re.findall(r"[a-z]+", name.lower()) if p not in PARTICLES]
    if not local or len(parts) < 2:
        return 0.0
    first, last = parts[0], parts[-1]
    forms = {first + last, first[0] + last, last + first, last + first[0], first, last}
    best = max(fuzz.ratio(local, f) for f in forms)
    for p in (first, last):
        if len(p) >= 4 and p in local:
            best = max(best, 85)
    return best / 100


# ---------------- small field helpers ----------------
OFFICE_RE = re.compile(r"(?i)\b(?:room|office|cabin)\b[^\n|]{0,40}")
RESEARCH_RE = re.compile(r"(?i)(?:research\s+(?:areas?|interests?)|specializ\w+|interests?)\s*[:\-–]\s*([^\n]{3,150})")
TITLED_NAME_RE = re.compile(r"\b(?:Dr|Prof|Professor|Mr|Mrs|Ms|Shri|Smt)\.?\s+((?:[A-Z][\w'’\-.]*\s*){2,4})")


def _extras(text: str) -> dict:
    off = OFFICE_RE.search(text)
    res = RESEARCH_RE.search(text)
    return {"phone": find_phone(text), "office": clean(off.group()) if off else "",
            "research_area": clean(res.group(1)) if res else ""}


# ---------------- tables ----------------
HEADER_WORDS = {
    "email": ("e-mail", "email", "mail"),
    "phone": ("phone", "tel", "mobile", "ext"),
    "role": ("designation", "position", "title", "role", "rank", "post"),
    "name": ("name", "faculty", "staff", "member"),
}


def _map_header(row: list[str]) -> dict:
    m = {}
    for i, cell in enumerate(row):
        c = clean(cell).lower()
        if not c or len(c) > 40:
            continue
        for key, words in HEADER_WORDS.items():
            if key not in m and any(w in c for w in words):
                m[key] = i
                break
    return m


def grid_contacts(grid: list[list[str]], url: str, method: str, links: list[str] | None = None) -> list[Contact]:
    col, start = {}, 0
    for i, row in enumerate(grid[:5]):
        m = _map_header(row)
        if len(m) >= 2 and ("name" in m or "email" in m):
            col, start = m, i + 1
            break
    out = []
    for idx in range(start, len(grid)):
        cells = [clean(c) for c in grid[idx]]
        if not any(cells):
            continue
        link = links[idx] if links and idx < len(links) else ""
        email_txt = cells[col["email"]] if col.get("email", 999) < len(cells) else ""
        emails = find_emails(email_txt) or find_emails(" ".join(cells))
        role = None
        if col.get("role", 999) < len(cells):
            role = detect_role(cells[col["role"]])
        if role is None:
            for c in cells:
                r = detect_role(c)
                if r and (role is None or r[1] < role[1]):
                    role = r
        if role is None:
            continue
        name = ""
        if col.get("name", 999) < len(cells):
            n = clean_name(cells[col["name"]])
            name = n if valid_name(n) else ""
        if not name:
            for c in cells:
                if find_emails(c):
                    continue
                n = clean_name(c)
                if valid_name(n):
                    name = n
                    break
        if not name:
            continue
        phone = find_phone(cells[col["phone"]]) if col.get("phone", 999) < len(cells) else ""
        phone = phone or find_phone(" ".join(cells))
        out.append(Contact(name=name, role=role[0], rank=role[1], method=method, source_url=url,
                           email=emails[0] if emails else "", phone=phone, profile_url=link))
    return out


def table_contacts(soup: BeautifulSoup, url: str) -> list[Contact]:
    out = []
    for table in soup.find_all("table"):
        grid, links = [], []
        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue
            row, link = [], ""
            for cell in cells:
                txt = clean(cell.get_text(" "))
                for a in cell.find_all("a", href=True):
                    h = a["href"]
                    if h.lower().startswith("mailto:"):
                        txt += " " + unquote(h[7:]).split("?")[0]
                    elif not h.lower().startswith(("tel:", "javascript:", "#")) and not link:
                        link = urljoin(url, h)
                row.append(txt)
            grid.append(row)
            links.append(link)
        out += grid_contacts(grid, url, "HTML_TABLE", links)
    return out


# ---------------- structured HTML (cards / profile blocks) ----------------
_STOP_NAMES = ("body", "html", "[document]")


def _emails_in(node) -> set[str]:
    es = set(find_emails(node.get_text(" ")))
    anchors = ([node] if node.name == "a" else []) + node.select('a[href^="mailto:" i]')
    for a in anchors:
        href = a.get("href", "")
        if href.lower().startswith("mailto:"):
            es |= set(find_emails(unquote(href[7:]).split("?")[0]))
    return es


def _container(el):
    """Largest ancestor that still holds exactly one email: that is one person's card."""
    node = el
    while node.parent is not None and node.parent.name not in _STOP_NAMES:
        parent = node.parent
        if len(_emails_in(parent)) > 1 or len(parent.get_text(" ", strip=True)) > 700:
            break
        node = parent
    return node


def _card_contact(node, email: str, url: str) -> Contact | None:
    lines = [clean(x) for x in node.get_text("\n").split("\n") if clean(x)]
    joined = " ".join(lines)
    name = ""
    for el in node.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "strong", "b", "a"]):
        if el.name == "a" and el.get("href", "").lower().startswith(("mailto:", "tel:")):
            continue
        n = clean_name(el.get_text(" "))
        if valid_name(n):
            name = n
            break
    if not name:
        for line in lines:
            for seg in re.split(r"\s[-–—]\s|[|,:;/]", line):
                n = clean_name(seg)
                if valid_name(n):
                    name = n
                    break
            if name:
                break
    if not name:
        m = TITLED_NAME_RE.search(joined)
        if m and valid_name(clean_name(m.group(1))):
            name = clean_name(m.group(1))
    if not name:
        return None
    roles = [r for r in (detect_role(l) for l in lines + [joined]) if r]
    if not roles:
        return None
    role = min(roles, key=lambda r: r[1])
    profile = ""
    for a in node.find_all("a", href=True):
        if not a["href"].lower().startswith(("mailto:", "tel:", "javascript:", "#")) \
                and clean_name(a.get_text(" ")) == name:
            profile = urljoin(url, a["href"])
            break
    ex = _extras(joined)
    tel = node.select_one('a[href^="tel:" i]')
    phone = clean(unquote(tel["href"][4:])) if tel else ex["phone"]
    return Contact(name=name, role=role[0], rank=role[1], method="STRUCTURED_HTML", source_url=url,
                   email=email, phone=phone, office=ex["office"], profile_url=profile,
                   research_area=ex["research_area"])


def structured_contacts(soup: BeautifulSoup, url: str) -> list[Contact]:
    carriers = list(soup.select('a[href^="mailto:" i]'))
    for s in soup.find_all(string=lambda x: x and EMAIL_RE.search(deobfuscate(x))):
        if s.parent is not None:
            carriers.append(s.parent)
    out, used = [], set()
    for el in carriers:
        emails = _emails_in(el)
        if not emails:
            continue
        node = _container(el)
        if id(node) in used:
            continue
        used.add(id(node))
        c = _card_contact(node, sorted(emails)[0], url)
        if c:
            out.append(c)
    return out


# ---------------- deterministic text ----------------
def _name_from_line(line: str) -> str:
    s = EMAIL_RE.sub("|", line)
    s = PHONE_RE.sub("|", s)
    s = SUBHEAD_RE.sub("|", s)
    s = ROLE_ANY.sub("|", s)
    for seg in re.split(r"\s[-–—]\s|[|,:;/()]", s):
        n = clean_name(seg)
        if valid_name(n):
            return n
    return ""


def text_contacts(text: str, url: str, reg: str, method: str = "DETERMINISTIC") -> list[Contact]:
    """Plain-text lines are the loosest source, so the bar is high:
    - Professor / Associate / Assistant / Other need an email that resembles the person's name.
    - Head / Chair need a name-matched email, a Dr./Prof. title, or a label directly beside a short name line.
    - Lines that read like news (alumni, awards, lectures, congratulations) never count without an email."""
    lines = [clean(l) for l in deobfuscate(text).splitlines()]
    lines = [l for l in lines if l]
    out = []
    for i, line in enumerate(lines):
        role = detect_role(line)
        if not role or len(line) > 200:
            continue
        src, label_only = line, False
        name = _name_from_line(line)
        if not name and has_title(line):
            m = TITLED_NAME_RE.search(line)
            if m:
                n = clean_name(m.group(1))
                name = n if valid_name(n) else ""
        if not name:
            for j in (i - 1, i + 1):
                if 0 <= j < len(lines) and len(lines[j]) <= 80:
                    name = _name_from_line(lines[j])
                    if name:
                        src, label_only = lines[j], len(lines[j]) <= 40
                        break
        if not name:
            continue
        window = " ".join(lines[max(0, i - 2): i + 7])
        cands = [e for e in dict.fromkeys(find_emails(window)) if domain_ok(e, reg) and not is_generic(e)]
        scored = sorted(((email_matches_name(e, name), e) for e in cands), reverse=True)
        email = scored[0][1] if scored and scored[0][0] >= 0.6 else ""  # never bind on proximity alone
        if role[1] >= 2 and not email:
            continue  # a title in prose is not a roster entry
        if not email and NEWS_CUE_RE.search(f"{line} {src}"):
            continue  # alumni / award / lecture news
        structural = role[1] <= 1 and label_only
        if not (email or has_title(src) or structural):
            continue  # a bare capitalised phrase is not evidence of a person
        ex = _extras(window)
        phone = find_phone(line) or ex.get("phone", "")
        out.append(Contact(name=name, role=role[0], rank=role[1], method=method, source_url=url,
                           email=email, phone=phone, office=ex.get("office", ""),
                           research_area=ex.get("research_area", "")))
    return out


# ---------------- merge / validate ----------------
def finalize(contacts: list[Contact], reg: str) -> list[Contact]:
    seen: dict[str, Contact] = {}
    for c in contacts:
        c.name = clean_name(c.name)
        if not valid_name(c.name):
            continue
        if c.email:
            e = c.email.lower()
            c.email = e if (domain_ok(e, reg) and not is_generic(e)) else ""  # veto, not downgrade
        prev = seen.get(c.name.lower())
        if prev is None:
            seen[c.name.lower()] = c
            continue
        if c.rank < prev.rank:
            prev.role, prev.rank, prev.role_method = c.role, c.rank, c.role_method
        for f in ("email", "phone", "office", "profile_url", "research_area"):
            if not getattr(prev, f) and getattr(c, f):
                setattr(prev, f, getattr(c, f))
        if METHOD_ORDER.get(c.method, 9) < METHOD_ORDER.get(prev.method, 9):
            prev.method = c.method
    return list(seen.values())


def best_contact(contacts: list[Contact]) -> Contact | None:
    ok = [c for c in contacts if c.rank < 99 and c.name]
    if not ok:
        return None
    return sorted(ok, key=lambda c: (c.rank, 0 if c.email else 1, METHOD_ORDER.get(c.method, 9)))[0]


# ---------------- page-level entry points ----------------
def html_text(soup: BeautifulSoup) -> str:
    """Line-preserving text. Mutates the soup, so call after table/structured extraction."""
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for el in soup.find_all(["p", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5",
                             "h6", "section", "article", "ul", "ol", "table"]):
        el.append("\n")
    return soup.get_text(" ")


def link_profiles(contacts: list[Contact], soup: BeautifulSoup, url: str) -> None:
    """Link contacts to individual profile/bio pages or bio PDFs found in their container or by name."""
    all_links = soup.find_all("a", href=True)
    for c in contacts:
        if c.profile_url:
            continue
        c_name = clean_name(c.name).lower()
        parts = [p for p in c_name.split() if len(p) >= 3]
        if not parts:
            continue

        # Strategy 1: Look for bio / profile link in the same parent card/container
        parent = soup.find(string=lambda s: s and c.name in s)
        if parent:
            container = parent.find_parent(["div", "tr", "li", "article", "section"])
            if container:
                for a in container.find_all("a", href=True):
                    href = a["href"].strip()
                    if href.lower().startswith(("mailto:", "tel:", "javascript:", "#")):
                        continue
                    txt = a.get_text(" ").strip().lower()
                    if (
                        href.lower().endswith(".pdf")
                        or any(k in txt for k in ("bio", "profile", "cv", "resume", "view"))
                        or any(p in href.lower() for p in parts)
                    ):
                        c.profile_url = urljoin(url, href)
                        break

        # Strategy 2: If not found in container, check all links for filename/anchor matching name
        if not c.profile_url:
            for a in all_links:
                href = a["href"].strip()
                if href.lower().startswith(("mailto:", "tel:", "javascript:", "#")):
                    continue
                full_str = (href + " " + a.get_text(" ")).lower()
                if parts[-1] in full_str and (len(parts) == 1 or any(p in full_str for p in parts[:-1])):
                    c.profile_url = urljoin(url, href)
                    break


def extract_html(html: str, url: str, reg: str) -> tuple[list[Contact], str]:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    found = table_contacts(soup, url) + structured_contacts(soup, url)
    text = html_text(soup)
    found += text_contacts(text, url, reg)
    link_profiles(found, soup, url)
    return finalize(found, reg), text


def extract_pdf(content: bytes, url: str, reg: str) -> tuple[list[Contact], str]:
    text = pdf_text(content)
    found = []
    if find_emails(text):  # tabular listings: prefer real table structure
        for grid in pdf_tables(content):
            found += grid_contacts(grid, url, "PDF_TABLE")
    found += text_contacts(text, url, reg)
    return finalize(found, reg), text


# ---------------- Qwen fallback (validated, never trusted blindly) ----------------
class _LC(BaseModel):
    name: str
    role: str = ""
    email: str = ""
    phone: str = ""


class _LCs(BaseModel):
    contacts: list[_LC] = []


def role_windows(text: str, reg: str, max_chars: int = 3500) -> str:
    """Small relevant context for Qwen. Empty string means a valid answer is impossible: do not call."""
    lines = [clean(l) for l in text.splitlines() if clean(l)]
    hi, lo = [], []
    for i, l in enumerate(lines):
        r = detect_role(l)
        if r and len(l) <= 200:
            (hi if r[1] <= 1 else lo).append(i)
        elif any(domain_ok(e, reg) for e in find_emails(l)):
            lo.append(i)
    keep, size = set(), 0
    for i in hi + lo:
        win = [j for j in range(max(0, i - 2), min(len(lines), i + 4)) if j not in keep]
        add = sum(len(lines[j]) + 1 for j in win)
        if size + add > max_chars:
            continue
        keep.update(win)
        size += add
    return "\n".join(lines[j] for j in sorted(keep))


def _role_near_name(lines: list[str], name: str, rank: int) -> bool:
    """The claimed role (or a stronger one) must sit within 1 line above / 2 lines below the line
    that holds the person's name as text. Email lines do not count as the name line."""
    target = " ".join(name.lower().split())
    for i, l in enumerate(lines):
        if "@" in l or target not in " ".join(l.lower().split()):
            continue
        for w in lines[max(0, i - 1): i + 3]:
            r = detect_role(w)
            if r and r[1] <= rank:
                return True
    return False


def _named_after(ctx: str, name: str) -> bool:
    """'Strachey Professor of Computing', 'Strachey Lecture', 'Strachey Building': a name used
    for a chair, lecture or building is not a living faculty member."""
    last = re.escape(name.split()[-1])
    pat = (rf"\b{last}[ ]+(?:professor|chair)[ ]+(?:of|in|at)\b"
           rf"|\b{last}[ ]+(?:lecture|lectures|building|prize|award|institute|centre|center)\b")
    return bool(re.search(pat, ctx, re.I))


def _has_evidence(lines: list[str], name: str, email: str) -> bool:
    """A Qwen-found person needs real evidence: a matching email, a Dr./Prof. title on the name's
    line, or a Head/Chair role on that same line."""
    if email:
        return True
    target = " ".join(name.lower().split())
    for l in lines:
        if "@" in l or target not in " ".join(l.lower().split()):
            continue
        if has_title(l):
            return True
        r = detect_role(l)
        if r and r[1] <= 1:
            return True
    return False


def llm_contacts(pages: list[tuple[str, str]], reg: str, max_pages: int = 2) -> list[Contact]:
    out = []
    for url, text in pages[:max_pages]:
        ctx = role_windows(text, reg)
        if not ctx:
            continue
        res = llm.ask(
            "Extract people who are the Head of Department, Chair, or a Professor of the department. "
            "Copy names, roles and emails EXACTLY as written. Never invent or guess. Leave a field empty "
            "if it is not in the text. At most 5 people.\n\nTEXT:\n" + ctx, _LCs)
        if not res:
            continue
        ctx_lines = [clean(l) for l in deobfuscate(ctx).splitlines() if clean(l)]
        ctx_norm = " ".join(deobfuscate(ctx).lower().split())
        ctx_emails = set(find_emails(ctx))
        for c in res.contacts[:5]:
            name = clean_name(c.name)
            role = detect_role(c.role)
            if not valid_name(name) or " ".join(name.lower().split()) not in ctx_norm or not role:
                continue
            if not _role_near_name(ctx_lines, name, role[1]) or _named_after(ctx, name):
                continue  # role not supported by the text, or the "name" is a named chair/lecture
            email = ""
            for e in find_emails(c.email):
                if (e in ctx_emails and domain_ok(e, reg) and not is_generic(e)
                        and email_matches_name(e, name) >= 0.6):
                    email = e  # must exist in the text AND resemble this person's name
            if not _has_evidence(ctx_lines, name, email):
                continue
            out.append(Contact(name=name, role=role[0], rank=role[1], method="LLM", source_url=url,
                               email=email, phone=find_phone(c.phone) if c.phone in ctx else ""))
    return finalize(out, reg)
