"""HTTP fetching with a small on-disk cache and an optional Playwright fallback."""
import hashlib
import json
import re
from dataclasses import dataclass

import requests
import urllib3
from loguru import logger
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from . import config

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
_session = requests.Session()
_session.headers.update({"User-Agent": config.USER_AGENT, "Accept-Language": "en;q=0.9"})
_failed: set[str] = set()  # URLs that failed this process: don't pay the timeout twice
_pw_disabled = False       # set when Chromium is missing: don't retry on every page

# Statuses that usually mean "this site blocks scripted requests" rather than "page does not exist"
BLOCK_STATUSES = {401, 403, 406, 429, 500, 502, 503}
_BLOCK_MARKERS = (
    "access denied", "403 forbidden", "forbidden", "just a moment", "attention required",
    "request blocked", "verify you are human", "unusual traffic", "captcha", "bot detected",
)

_STEALTH_JS = """\
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
if (!window.chrome) window.chrome = {runtime: {}};
"""

_NAV_KEYWORDS_RE = re.compile(
    r"academics?|departments?|schools?|facult(?:y|ies)|programs?|programmes?|colleges?",
    re.I,
)


@dataclass
class Page:
    url: str
    status: int
    content: bytes
    content_type: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400

    @property
    def is_pdf(self) -> bool:
        return (
            "pdf" in self.content_type.lower()
            or self.content[:5] == b"%PDF-"
            or self.url.lower().split("?")[0].endswith(".pdf")
        )

    @property
    def text(self) -> str:
        if self.is_pdf:
            return ""
        try:
            return self.content.decode("utf-8")
        except UnicodeDecodeError:
            return self.content.decode("cp1252", errors="replace")


def looks_blocked(html: str) -> bool:
    """True if the 'page' is really a bot-protection wall: short, with a block marker in its title or
    opening text. Long real pages are never flagged."""
    head = html[:20000]
    title_m = re.search(r"<title[^>]*>(.*?)</title>", head, flags=re.S | re.I)
    title = " ".join(title_m.group(1).split()).lower() if title_m else ""
    body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", head, flags=re.S | re.I)
    visible = " ".join(re.sub(r"<[^>]+>", " ", body).split()).lower()
    if len(visible) >= 2500:
        return False
    return any(m in title or m in visible[:600] for m in _BLOCK_MARKERS)


def _get_once(url: str, timeout):
    try:
        return _session.get(url, timeout=timeout, allow_redirects=True)
    except requests.exceptions.SSLError:
        # Many university sites have broken certificates; we only read public pages
        return _session.get(url, timeout=timeout, allow_redirects=True, verify=False)


_get_retry = retry(
    stop=stop_after_attempt(2),
    wait=wait_fixed(1),
    retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
    reraise=True,
)(_get_once)


def _cache_base(url: str):
    return config.CACHE_DIR / hashlib.sha1(url.encode()).hexdigest()


def fetch(url: str, cache: bool = True, timeout=None, retries: bool = True) -> Page | None:
    if url in _failed:
        return None
    base = _cache_base(url)
    meta_f, bin_f = base.with_suffix(".json"), base.with_suffix(".bin")
    if cache and config.HTTP_CACHE and meta_f.exists() and bin_f.exists():
        try:
            m = json.loads(meta_f.read_text())
            return Page(m["url"], m["status"], bin_f.read_bytes(), m["content_type"])
        except Exception:
            pass
    try:
        r = (_get_retry if retries else _get_once)(url, timeout or config.HTTP_TIMEOUT)
    except Exception as e:
        logger.debug(f"fetch failed {url}: {str(e)[:150]}")
        _failed.add(url)
        return None
    if len(r.content) > config.MAX_DOWNLOAD_BYTES:
        return None
    page = Page(r.url, r.status_code, r.content, r.headers.get("Content-Type", ""))
    if not page.ok:
        logger.debug(f"fetch {url} -> HTTP {page.status}")
    if cache and config.HTTP_CACHE and page.ok:
        bin_f.write_bytes(page.content)
        meta_f.write_text(json.dumps({"url": page.url, "status": page.status,
                                      "content_type": page.content_type}))
    return page


_rendered_cache: dict[str, Page | None] = {}


def fetch_rendered(url: str) -> Page | None:
    """Playwright fallback for JS-rendered pages. Returns None if Playwright/Chromium is missing, or
    if the browser itself only received a bot-protection page.
    Waits for the DOM, gives the network a short chance to go quiet, then reads whatever is there:
    busy sites never reach 'networkidle', so we must not wait for it."""
    global _pw_disabled
    if _pw_disabled:
        return None
    if url in _rendered_cache:
        return _rendered_cache[url]
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _pw_disabled = True
        logger.warning("playwright is not installed; JS-only pages will be skipped")
        return None
    page = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=config.USER_AGENT,
                ignore_https_errors=True,
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
            )
            ctx.add_init_script(_STEALTH_JS)
            pg = ctx.new_page()
            pg.goto(url, wait_until="domcontentloaded", timeout=30_000)
            try:
                pg.wait_for_load_state("networkidle", timeout=8_000)
            except Exception:
                pass  # busy site: take what has rendered so far
            pg.wait_for_timeout(1_500)
            html = pg.content()
            browser.close()
        if looks_blocked(html):
            logger.warning(f"the browser also received a bot-protection page for {url}")
        else:
            page = Page(url, 200, html.encode("utf-8"), "text/html")
    except Exception as e:
        msg = str(e)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            _pw_disabled = True
            logger.warning("Chromium is not installed. Run: playwright install chromium "
                           "(JS-only pages will be skipped until then)")
        else:
            logger.warning(f"rendered fetch failed for {url}: {msg[:200]}")
    _rendered_cache[url] = page
    return page


def fetch_robust(url: str) -> Page | None:
    """Plain fetch first. Escalates to a real browser if the site looks like it blocks scripted
    requests -- either by status code (401/403/406/429/5xx), or by serving a 200 OK page that is
    actually a bot-protection challenge wall. The second case matters: many anti-bot systems
    deliberately return 200 (not 403) specifically so that scripts which only check status codes
    sail straight through with garbage interstitial content instead of the real page -- status
    alone previously made that indistinguishable from a normal, successful fetch.
    A 404 is a real answer and is never rendered. If the browser fails too (or also only gets a
    challenge page), the original response is returned so callers can still report it."""
    page = fetch(url)
    challenge_page = page is not None and page.ok and not page.is_pdf and looks_blocked(page.text)
    if page is not None and not challenge_page and (page.ok or page.status not in BLOCK_STATUSES):
        return page
    rendered = fetch_rendered(url)
    return rendered or page


def crawl_js_navigation(url: str) -> list[tuple[str, str]]:
    """Click JS navigation menus (Academics, Departments …) and collect the
    links that only appear after the click.  Returns ``[(abs_url, text)]``."""
    global _pw_disabled
    if _pw_disabled:
        return []
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _pw_disabled = True
        return []

    links: list[tuple[str, str]] = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=config.USER_AGENT,
                ignore_https_errors=True,
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
            )
            ctx.add_init_script(_STEALTH_JS)
            pg = ctx.new_page()
            pg.goto(url, wait_until="domcontentloaded", timeout=30_000)
            try:
                pg.wait_for_load_state("networkidle", timeout=8_000)
            except Exception:
                pass
            pg.wait_for_timeout(1_500)

            # Click navigation items that look like academic hub menus.
            clicked: set[str] = set()
            for sel in ("a", "button", "[role='menuitem']",
                        "[class*='menu'] > *", "[class*='nav'] a",
                        "li > a", "span"):
                try:
                    elements = pg.query_selector_all(sel)
                except Exception:
                    continue
                for el in elements:
                    try:
                        text = (el.text_content() or "").strip()
                        if (not text or len(text) > 40
                                or text.lower() in clicked):
                            continue
                        if not _NAV_KEYWORDS_RE.search(text):
                            continue
                        clicked.add(text.lower())
                        el.scroll_into_view_if_needed(timeout=2000)
                        el.click(timeout=3000)
                        pg.wait_for_timeout(1_500)
                    except Exception:
                        continue

            html = pg.content()
            browser.close()

        # Parse links from the fully-rendered, interacted-with page.
        from bs4 import BeautifulSoup
        from urllib.parse import urldefrag, urljoin

        soup = BeautifulSoup(html, "lxml")
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"].strip()
            if href.lower().startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            abs_url = urldefrag(urljoin(url, href))[0]
            if abs_url.startswith("http"):
                link_text = re.sub(r"\s+", " ", a_tag.get_text(" ")).strip()
                links.append((abs_url, link_text))

    except Exception as e:
        msg = str(e)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            _pw_disabled = True
            logger.warning("Chromium not installed for JS navigation crawling")
        else:
            logger.warning(f"JS navigation crawl failed for {url}: {msg[:200]}")

    logger.info(f"[js-nav] {url}: found {len(links)} links via JS navigation")
    return links