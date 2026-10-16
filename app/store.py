"""SQLite run state. Excel/CSV are only output artifacts."""
import hashlib
import json
import sqlite3
import time

from . import config
from .models import Result

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  run_id TEXT, key TEXT, university TEXT, discipline TEXT, geo TEXT, country TEXT, website TEXT,
  status TEXT DEFAULT 'Pending', result_json TEXT, error TEXT, updated_at REAL,
  PRIMARY KEY (run_id, key)
)"""
# Statuses that mean "finished, but with no contact": skipped on resume, retried with --retry-failed
FINAL_NOT_FOUND = ["Contact Not Found", "Field Not Found", "Website Not Found",
                   "Ambiguous Department", "Department Page Blocked"]


def make_key(it: dict) -> str:
    raw = "|".join(str(it.get(k, "")).strip().lower() for k in ("university", "discipline", "geo", "country"))
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


class Store:
    def __init__(self, path=None):
        self.conn = sqlite3.connect(str(path or config.DB_PATH))
        self.conn.execute(SCHEMA)
        self.conn.commit()

    def add_items(self, run_id: str, items: list[dict]):
        for it in items:
            it["key"] = make_key(it)
            self.conn.execute(
                "INSERT OR IGNORE INTO items (run_id,key,university,discipline,geo,country,website,"
                "status,updated_at) VALUES (?,?,?,?,?,?,?,'Pending',?)",
                (run_id, it["key"], it["university"], it["discipline"], it.get("geo", ""),
                 it.get("country", ""), it.get("website", ""), time.time()))
        self.conn.commit()

    def pending(self, run_id: str, retry_failed: bool = False) -> list[dict]:
        statuses = ["Pending", "Error"] + (FINAL_NOT_FOUND if retry_failed else [])
        q = ("SELECT key,university,discipline,geo,country,website FROM items WHERE run_id=? "
             f"AND status IN ({','.join('?' * len(statuses))}) ORDER BY rowid")
        rows = self.conn.execute(q, [run_id, *statuses]).fetchall()
        return [dict(zip(("key", "university", "discipline", "geo", "country", "website"), r)) for r in rows]

    def save(self, run_id: str, key: str, res: Result):
        self.conn.execute(
            "UPDATE items SET status=?, result_json=?, error=?, updated_at=? WHERE run_id=? AND key=?",
            (res.status, json.dumps(res.to_dict()), res.error, time.time(), run_id, key))
        self.conn.commit()

    def results(self, run_id: str) -> list[Result]:
        rows = self.conn.execute(
            "SELECT result_json FROM items WHERE run_id=? AND result_json IS NOT NULL ORDER BY rowid",
            (run_id,)).fetchall()
        return [Result(**json.loads(r[0])) for r in rows]