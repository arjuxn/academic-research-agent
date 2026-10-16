"""Rule-based confidence: driven by evidence quality, not by the LLM."""
from urllib.parse import urlparse

from .contacts import email_matches_name
from .models import Contact

METHOD_BASE = {"HTML_TABLE": 0.90, "PDF_TABLE": 0.90, "STRUCTURED_HTML": 0.80,
               "DETERMINISTIC": 0.65, "LLM": 0.45}


def _c(x: float) -> float:
    return round(max(0.0, min(1.0, x)), 2)


def score(c: Contact, dept_url: str, dept_conf: float) -> dict:
    base = METHOD_BASE.get(c.method, 0.4)
    name_conf = base
    if c.email:
        sim = email_matches_name(c.email, c.name)
        email_conf = base + (0.10 if sim >= 0.7 else 0.0) - (0.20 if sim < 0.4 else 0.0)
    else:
        email_conf = 0.0
    role_conf = METHOD_BASE.get(c.role_method, 0.4) + (0.05 if c.rank <= 1 else 0.0)
    sp, dp = urlparse(c.source_url), urlparse(dept_url)
    if sp.netloc == dp.netloc and sp.path.startswith(dp.path.rstrip("/")):
        loc = 1.0
    elif sp.netloc == dp.netloc:
        loc = 0.85
    else:
        loc = 0.6
    return {
        "name_confidence": _c(name_conf),
        "email_binding_confidence": _c(email_conf),
        "department_match_confidence": _c(dept_conf * loc),
        "role_confidence": _c(role_conf),
    }