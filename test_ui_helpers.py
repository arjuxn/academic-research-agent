import tempfile
from pathlib import Path

from ui import load_batch_items, safe_slug
from app.http import looks_blocked


def test_load_batch_items():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "in.csv"
        p.write_text("University Name,GEO,Country/Region,Discipline,Website\\n"
                     "Example University,West,India,,https://example.edu\\n", encoding="utf-8")
        rows = load_batch_items(p, "Computer Science")
        assert len(rows) == 1
        assert rows[0]["university"] == "Example University"
        assert rows[0]["discipline"] == "Computer Science"
        assert rows[0]["website"] == "https://example.edu"


def test_safe_slug():
    assert safe_slug("Computer Science + Pune") == "computer-science-pune"


def test_http_200_challenge_detected():
    html = "<html><head><title>Just a moment</title></head><body>Verify you are human</body></html>"
    assert looks_blocked(html)
