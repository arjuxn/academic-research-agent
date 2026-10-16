"""Mode 2 discovery: Wikidata (name + website) first, search-engine snippets as a supplement."""
import re
from collections import Counter

import requests
from loguru import logger
from pydantic import BaseModel
from rapidfuzz import fuzz

from . import llm

WD_API = "https://www.wikidata.org/w/api.php"
SPARQL = "https://query.wikidata.org/sparql"
HEADERS = {"User-Agent": "UniContactPOC/0.1 (local research tool)"}
UNI_RE = re.compile(
    r"\b((?:[A-Z][\w&'’.\-]*\s+){0,5}(?:University|Institute of Technology|Institute|College|Polytechnic)"
    r"(?:\s+of\s+(?:[A-Z][\w&'’.\-]*\s*){1,4})?)")


def _geo_qid(geo: str) -> str | None:
    try:
        r = requests.get(WD_API, params={"action": "wbsearchentities", "search": geo, "language": "en",
                                         "type": "item", "limit": 5, "format": "json"},
                         headers=HEADERS, timeout=15)
        hits = r.json().get("search", [])
    except Exception:
        return None
    for h in hits:
        d = (h.get("description") or "").lower()
        if any(w in d for w in ("state", "province", "region", "city", "district", "county",
                                "territory", "country", "prefecture", "division")):
            return h["id"]
    return hits[0]["id"] if hits else None


def _wikidata(geo: str, limit: int) -> list[dict]:
    qid = _geo_qid(geo)
    if not qid:
        return []
    query = f"""
    SELECT DISTINCT ?u ?uLabel ?site WHERE {{
      VALUES ?type {{ wd:Q3918 wd:Q875538 wd:Q15936437 wd:Q189004 }}
      ?u wdt:P31 ?type . ?u wdt:P131* wd:{qid} .
      OPTIONAL {{ ?u wdt:P856 ?site . }}
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
    }} LIMIT {limit * 3}"""
    try:
        r = requests.get(SPARQL, params={"query": query, "format": "json"}, headers=HEADERS, timeout=70)
        rows = r.json()["results"]["bindings"]
    except Exception as e:
        logger.warning(f"wikidata sparql failed: {e}")
        return []
    out, seen = [], set()
    for b in rows:
        name = b["uLabel"]["value"]
        if re.fullmatch(r"Q\d+", name) or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append({"name": name, "website": b.get("site", {}).get("value", "")})
    return out


class _Names(BaseModel):
    names: list[str] = []


def _search(discipline: str, geo: str, limit: int) -> list[dict]:
    try:
        from ddgs import DDGS
        ddg = DDGS()
        texts = []
        for q in (f"universities in {geo} offering {discipline}", f"list of universities in {geo}",
                  f"best colleges in {geo} {discipline} department"):
            for r in ddg.text(q, max_results=10) or []:
                texts.append(f"{r.get('title', '')}. {r.get('body', '')}")
    except Exception as e:
        logger.debug(f"discovery search failed: {e}")
        return []
    counts = Counter(m.strip() for t in texts for m in UNI_RE.findall(t) if 2 <= len(m.split()) <= 8)
    names = [n for n, _ in counts.most_common(limit * 2)]
    if names and llm.available():  # optional Qwen cleanup; result must be a subset of the input
        res = llm.ask("Keep only real university/college/institute names (remove duplicates, listicle "
                      "titles and fragments). Return names exactly as given.\n" + "\n".join(names), _Names)
        if res:
            allowed = {n.lower() for n in names}
            kept = [n for n in res.names if n.lower() in allowed]
            names = kept or names
    return [{"name": n, "website": ""} for n in names]


def discover_universities(discipline: str, geo: str, limit: int = 20) -> list[dict]:
    found = _wikidata(geo, limit)
    if len(found) < limit:
        for e in _search(discipline, geo, limit):
            if not any(fuzz.token_sort_ratio(e["name"], f["name"]) >= 90 for f in found):
                found.append(e)
    return found[:limit]