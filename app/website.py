
"""Stage 1: discover official institutional websites and rank candidates."""
from dataclasses import dataclass
import re

import requests
from bs4 import BeautifulSoup
from loguru import logger
from rapidfuzz import fuzz

from . import config
from .http import fetch, looks_blocked
from .util import _ext, clean, norm, registered_domain, root_url, tokens

WD_API = "https://www.wikidata.org/w/api.php"
WD_HEADERS = {"User-Agent": "UniContactPOC/0.1 (local research tool)"}

GENERIC_WORDS = {
    "university", "college", "institute", "school", "deemed", "be",
    "academy", "polytechnic",
}
QUALIFIERS = {
    "distance", "online", "centre", "center", "open", "extension",
    "alumni", "admission", "admissions", "hospital", "medical",
    "dental", "law", "pharmacy", "hostel", "library", "press",
    "faculty", "department", "research", "foundation", "cdoe",
    "sde", "ide", "idol", "dde", "sdoe",
}
NOT_INSTITUTION_QIDS = {
    "Q5", "Q486972", "Q515", "Q6256", "Q532", "Q4830453", "Q15617994",
}


def is_blocked(reg: str) -> bool:
    return any(reg == domain or reg.endswith("." + domain)
               for domain in config.BLOCKED_DOMAINS)


def query_name(name: str) -> str:
    name = re.sub(r"\([^)]*\)", " ", name)
    name = re.sub(r"(?i)\bdeemed(\s+to\s+be)?\b", " ", name)
    return clean(name)


def core(name: str) -> str:
    return " ".join(t for t in tokens(name) if t not in GENERIC_WORDS)


def _compact(value: str) -> str:
    """Normalize text for comparisons where domains concatenate words."""
    return "".join(tokens(value))


def name_matches(name: str, text: str) -> bool:
    """Require meaningful institution-name evidence in the supplied text."""
    wanted = tokens(query_name(name))
    available = tokens(text)

    if not wanted or not available:
        return False

    compact_text = _compact(text)
    initials = "".join(w[0] for w in available if w)

    # A name can appear as separate words in a page title, as a concatenated domain,
    # or as initials/acronyms (e.g. 'DJ Sanghvi' matching 'Dwarkadas J. Sanghvi' / 'DJSCE').
    for token in wanted:
        if any(fuzz.ratio(token, candidate) >= 88 for candidate in available):
            continue
        if len(token) >= 2 and token in compact_text:
            continue
        if len(token) >= 2 and (token in initials or initials.startswith(token)):
            continue
        return False

    return True


def _penalty(name: str, text: str) -> int:
    return len((set(tokens(text)) & QUALIFIERS) - set(tokens(name)))


def _site_text(root: str) -> str | None:
    """Return identity evidence from the candidate website itself."""
    page = fetch(root)

    if not page or not page.ok or page.is_pdf or looks_blocked(page.text):
        return None

    soup = BeautifulSoup(page.text, "lxml")
    title = clean(soup.title.get_text()) if soup.title else ""

    site_meta = soup.find("meta", attrs={"property": "og:site_name"})
    site_name = clean(site_meta.get("content", "")) if site_meta else ""

    description_meta = soup.find("meta", attrs={"name": "description"})
    description = (
        clean(description_meta.get("content", ""))
        if description_meta else ""
    )

    ext = _ext(page.url)
    return (
        f"{title} {site_name} {description} "
        f"{ext.subdomain} {ext.domain}"
    )


def verify(root: str, name: str) -> bool:
    evidence = _site_text(root)
    if evidence and name_matches(name, evidence):
        return True

    # If the homepage is inaccessible, a strong institution identifier in the hostname
    # can still be useful evidence, but does not prove the page is reachable.
    hostname = root_url(root).split("://", 1)[-1].split("/", 1)[0]
    return _domain_matches_name(name, hostname)


def _domain_matches_name(name: str, hostname: str) -> bool:
    """Conservative domain-only match, not based on search snippets."""
    requested_core = core(query_name(name))
    if not requested_core:
        return False

    domain_text = hostname.lower().replace(".", " ").replace("-", " ")
    domain_compact = _compact(hostname)
    core_tokens = tokens(requested_core)

    # Every non-generic name token must be supported by the hostname.
    # This rejects directory domains such as pune.ws and admissionbharati.com
    # for a request for Bharati Vidyapeeth.
    for token in core_tokens:
        if token in tokens(domain_text):
            continue
        if len(token) >= 4 and token in domain_compact:
            continue
        if len(token) >= 2 and domain_compact.startswith(token):
            continue
        return False

    return True


def _wd(params: dict) -> dict:
    response = requests.get(
        WD_API,
        params={**params, "format": "json"},
        headers=WD_HEADERS,
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def _p31_qids(claims: dict) -> set[str]:
    output = set()

    for claim in claims.get("P31", []):
        try:
            output.add(claim["mainsnak"]["datavalue"]["value"]["id"])
        except (KeyError, TypeError):
            continue

    return output


def _wd_search_candidates(query: str) -> list[str]:
    hits = []
    try:
        hits = _wd({
            "action": "wbsearchentities",
            "search": query,
            "language": "en",
            "type": "item",
            "limit": 8,
        }).get("search", [])
    except Exception as exc:
        logger.debug(f"Wikidata search failed: {exc}")

    candidates = []

    for hit in hits:
        label = hit.get("label", "")
        matched = (hit.get("match") or {}).get("text", label)

        full_similarity = max(
            fuzz.ratio(norm(query), norm(matched)),
            fuzz.ratio(norm(query), norm(label)),
        )

        query_core = core(query)
        match_core = core(matched)
        core_similarity = (
            fuzz.ratio(query_core, match_core)
            if query_core and match_core else 0
        )

        if full_similarity >= 88 or core_similarity >= 92:
            candidates.append(hit["id"])

    # Fallback: Wikidata full-text search (handles informal names, acronyms)
    if not candidates:
        try:
            sr_hits = _wd({
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": 5,
            }).get("query", {}).get("search", [])
            for h in sr_hits:
                qid = h.get("title", "")
                if qid.startswith("Q") and qid not in candidates:
                    candidates.append(qid)
        except Exception as exc:
            logger.debug(f"Wikidata srsearch fallback failed: {exc}")

    return candidates


def wikidata_site(name: str) -> str | None:
    """Find the official URL recorded in Wikidata, even if access is blocked."""
    query = query_name(name)
    candidates = _wd_search_candidates(query)

    if not candidates:
        stripped = core(query)
        if stripped and stripped != norm(query):
            logger.debug(f"Retrying Wikidata with institution name: {stripped}")
            candidates = _wd_search_candidates(stripped)

    if not candidates:
        return None

    try:
        entities = _wd({
            "action": "wbgetentities",
            "ids": "|".join(candidates),
            "props": "claims",
        })["entities"]
    except Exception as exc:
        logger.debug(f"Wikidata entity lookup failed: {exc}")
        return None

    blocked_or_unreachable = None

    for qid in candidates:
        claims = entities.get(qid, {}).get("claims", {})

        if _p31_qids(claims) & NOT_INSTITUTION_QIDS:
            continue

        official_links = claims.get("P856", [])

        for claim in sorted(
            official_links,
            key=lambda item: item.get("rank") != "preferred",
        ):
            try:
                url = claim["mainsnak"]["datavalue"]["value"]
            except (KeyError, TypeError):
                continue

            root = root_url(url)
            domain = registered_domain(root)

            if not domain or is_blocked(domain):
                continue

            # Wikidata is useful provenance for an official URL. A blocked page should
            # remain a candidate, but the caller must not treat it as a working page.
            page = fetch(root)

            if page and page.ok and not page.is_pdf and not looks_blocked(page.text):
                return root

            blocked_or_unreachable = blocked_or_unreachable or root

    if blocked_or_unreachable:
        logger.warning(
            f"Wikidata lists {blocked_or_unreachable}, but the page could not be "
            "verified as accessible; retaining it as an unverified candidate"
        )
        return blocked_or_unreachable

    return None


@dataclass
class WebsiteCandidate:
    url: str
    score: float
    source: str
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "score": self.score,
            "source": self.source,
            "reason": self.reason,
        }


def _search_query_variants(
    name: str,
    country: str = "",
    geo: str = "",
    discipline: str = "",
) -> list[str]:
    queries = []

    def add(*parts):
        query = clean(" ".join(part for part in parts if part))
        key = norm(query)

        if query and key and all(norm(existing) != key for existing in queries):
            queries.append(query)

    add(name, country, "official website")

    if geo:
        add(name, geo, country, "official website")

    if discipline:
        add(name, discipline, country, "department official website")

    if geo and discipline:
        add(name, geo, discipline, country, "college department official website")

    return queries


def _host_key(url: str) -> str:
    hostname = root_url(url).split("://", 1)[-1].split("/", 1)[0]
    hostname = hostname.lower().split(":", 1)[0]
    return hostname[4:] if hostname.startswith("www.") else hostname


def search_candidates(
    name: str,
    country: str = "",
    geo: str = "",
    discipline: str = "",
) -> list[WebsiteCandidate]:
    """Rank search results using site identity, location, and discipline evidence."""
    try:
        from ddgs import DDGS
        search = DDGS()
    except Exception as exc:
        logger.debug(f"Search unavailable: {exc}")
        return []

    best_by_host: dict[str, tuple[float, int, WebsiteCandidate]] = {}
    order = 0

    for query in _search_query_variants(name, country, geo, discipline):
        try:
            results = search.text(query, max_results=10) or []
        except Exception as exc:
            logger.debug(f"Search failed for '{query}': {exc}")
            continue

        for result in results:
            url = result.get("href") or result.get("url")

            if not url or not url.lower().startswith(("http://", "https://")):
                order += 1
                continue

            domain = registered_domain(url)
            if not domain or is_blocked(domain):
                order += 1
                continue

            root = root_url(url)
            host_key = _host_key(root)

            if not host_key:
                order += 1
                continue

            # Crucially, do not use the result snippet to establish institution identity.
            # Snippets often describe the university while linking to an unrelated directory.
            site_evidence = _site_text(root)
            domain_match = _domain_matches_name(name, host_key)
            page_match = bool(
                site_evidence and name_matches(name, site_evidence)
            )

            if not page_match and not domain_match:
                order += 1
                continue

            # Search-result title/body may rank a site for campus and discipline, but cannot
            # independently make that site an official university website.
            search_text = clean(
                f"{result.get('title', '')} {result.get('body', '')}"
            )

            geo_tokens = set(tokens(geo))
            site_tokens = set(tokens(f"{root} {site_evidence or ''}"))
            geo_match = bool(geo_tokens and geo_tokens.issubset(site_tokens))

            discipline_tokens = set(tokens(discipline))
            discipline_match = bool(
                discipline_tokens
                and discipline_tokens.issubset(set(tokens(search_text)))
            )

            identity_strength = 0.0
            if page_match:
                identity_strength = 0.64
            elif domain_match:
                identity_strength = 0.58

            score = identity_strength
            reasons = []

            if geo_match:
                score += 0.22
                reasons.append("location verified on candidate site")
            elif geo:
                score -= 0.10
                reasons.append("location not verified on candidate site")

            if discipline_match:
                score += 0.08
                reasons.append("discipline mentioned in search result")

            if site_evidence:
                penalty = _penalty(name, site_evidence)
                score -= penalty * 0.04
                reasons.append(f"site penalty={penalty}")
            else:
                score -= 0.08
                reasons.append("site identity unverified due to access failure")

            score -= order * 0.002
            score = round(max(0.10, min(0.95, score)), 2)

            candidate = WebsiteCandidate(
                url=root,
                score=score,
                source="search",
                reason="; ".join(reasons),
            )

            old = best_by_host.get(host_key)
            if old is None or candidate.score > old[0]:
                best_by_host[host_key] = (candidate.score, order, candidate)

            order += 1

    ranked = sorted(
        best_by_host.values(),
        key=lambda item: (-item[0], item[1]),
    )
    return [item[2] for item in ranked]


def search_site(name: str, country: str = "") -> str | None:
    candidates = search_candidates(name, country)
    return candidates[0].url if candidates else None


def discover_websites(
    name: str,
    country: str = "",
    geo: str = "",
    discipline: str = "",
) -> list[WebsiteCandidate]:
    """Return ranked official-site candidates; inaccessible official sites remain visible."""
    candidates: list[WebsiteCandidate] = []
    seen_urls: set[str] = set()

    official_site = wikidata_site(name)

    if official_site:
        site_evidence = _site_text(official_site)
        is_verified = bool(
            site_evidence and name_matches(name, site_evidence)
        )

        if is_verified:
            score = 0.99
            reason = "Wikidata official URL; institution identity verified"
        else:
            score = 0.74
            reason = "Wikidata official URL; accessibility or identity unverified"

        candidates.append(
            WebsiteCandidate(
                url=official_site,
                score=score,
                source="wikidata",
                reason=reason,
            )
        )
        seen_urls.add(official_site.rstrip("/").lower())

    for candidate in search_candidates(name, country, geo, discipline):
        key = candidate.url.rstrip("/").lower()

        if key not in seen_urls:
            candidates.append(candidate)
            seen_urls.add(key)

    candidates.sort(key=lambda candidate: -candidate.score)
    return candidates


def resolve_website(
    name: str,
    country: str = "",
    geo: str = "",
    discipline: str = "",
) -> str | None:
    candidates = discover_websites(name, country, geo, discipline)

    if not candidates:
        return None

    best = candidates[0]
    logger.info(
        f"[site] {name} -> {best.url} "
        f"({best.source}; {best.reason})"
    )
    return best.url
