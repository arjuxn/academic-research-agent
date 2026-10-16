import re
from urllib.parse import urldefrag, urljoin, urlparse

import tldextract
from bs4 import BeautifulSoup

from . import config

# Bundled public-suffix snapshot only: no network call needed
_ext = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=str(config.CACHE_DIR / "tld"))

STOP = {"of", "the", "and", "for", "in", "at", "a", "an", "to"}


def registered_domain(url: str) -> str:
    """e.g. cse.iitd.ac.in -> iitd.ac.in. Uses the new property name when the installed
    tldextract has it, and the old one otherwise, so there is no deprecation warning."""
    r = _ext(url)
    if hasattr(type(r), "top_domain_under_public_suffix"):
        return r.top_domain_under_public_suffix
    return r.registered_domain


def host(url: str) -> str:
    return urlparse(url).netloc.lower().split(":")[0]


def root_url(url: str) -> str:
    p = urlparse(url if "//" in url else "https://" + url)
    return f"{p.scheme}://{p.netloc}"


def in_site(url: str, reg: str) -> bool:
    h = host(url)
    return h == reg or h.endswith("." + reg)


def clean(s) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def tokens(s: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in STOP]


def norm(s: str) -> str:
    return " ".join(tokens(s))


def extract_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Return [(absolute_url, anchor_text)] for http(s) links."""
    soup = BeautifulSoup(html, "lxml")
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urldefrag(urljoin(base_url, href))[0]
        if url.startswith("http"):
            out.append((url, clean(a.get_text(" "))))
    return out