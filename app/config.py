from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
CACHE_DIR = DATA_DIR / "cache"
DEBUG_DIR = DATA_DIR / "debug"
DB_PATH = DATA_DIR / "state.db"

for _d in (DATA_DIR, OUTPUT_DIR, CACHE_DIR, DEBUG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --- HTTP ---
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
HTTP_TIMEOUT = (8, 20)
HTTP_CACHE = True
MAX_DOWNLOAD_BYTES = 15_000_000
USE_PLAYWRIGHT = True

# --- Local LLM (Qwen3 4B via Ollama) ---
OLLAMA_MODEL = "qwen3:4b"
OLLAMA_KEEP_ALIVE = "30m"
OLLAMA_NUM_CTX = 4096
OLLAMA_TIMEOUT = 180

# --- Crawl limits / acceptance ---
MAX_SITEMAP_URLS = 30_000
MAX_FACULTY_PAGES = 6

# Try more ranked candidates because an institution can have separate campus
# and constituent-college websites.
MAX_WEBSITE_CANDIDATES = 5

DEPT_MIN_CONFIDENCE = 0.55

BLOCKED_DOMAINS = {
    "shiksha.com", "collegedunia.com", "careers360.com", "collegedekho.com", "getmyuni.com",
    "collegevidya.com", "jagranjosh.com", "naukri.com", "wikipedia.org", "wikidata.org",
    "topuniversities.com", "timeshighereducation.com", "usnews.com", "niche.com", "4icu.org",
    "edurank.org", "unipage.net", "mastersportal.com", "bachelorsportal.com", "cwur.org",
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com",
    "quora.com", "reddit.com", "indiatoday.in", "nirfindia.org", "hotcoursesabroad.com",
}

# Mailbox local-part tokens that denote a role/shared mailbox, never a person
GENERIC_MAILBOX_WORDS = {
    "info", "admission", "admissions", "contact", "office", "enquiry", "enquiries", "inquiry",
    "inquiries", "registrar", "admin", "administrator", "support", "webmaster", "helpdesk",
    "hod", "head", "chair", "chairman", "chairperson", "dept", "department", "secretary",
    "principal", "dean", "director", "mail", "hello", "general", "reception", "feedback",
    "noreply", "no", "reply", "careers", "hr",
    "ahead", "ahod", "dhead", "dhod", "assochead", "deputyhead", "cohead", "vicehead",
}

# Normalised (stop-words removed) discipline aliases. Keep these tight: precision over recall.
DISCIPLINE_ALIASES = {
    "computer science": {
        "cse", "cs", "eecs", "computer science engineering", "computer engineering",
        "computer sciences", "electrical engineering computer science",
    },
    "information technology": {"it", "information science"},
    "electrical engineering": {"ee", "eee", "electrical", "electrical electronics"},
    "mechanical engineering": {"me", "mech", "mechanical"},
    "civil engineering": {"ce", "civil"},
    "mathematics": {"math", "maths", "mathematical sciences"},
    "physics": {"phy", "physical sciences"},
    "chemistry": {"chem"},
    "biology": {"biosciences", "life sciences", "biological sciences", "biotechnology"},
    "business administration": {"management", "mba", "business", "commerce"},
    "economics": {"econ", "economic sciences"},
}