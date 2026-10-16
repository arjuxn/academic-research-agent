"""Structured per-university debug traces for the staged pipeline."""
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s[:80] or "unknown"


def _safe(value: Any):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe(v) for v in value]
    if hasattr(value, "to_dict"):
        return _safe(value.to_dict())
    if hasattr(value, "__dict__"):
        return _safe(vars(value))
    return str(value)


@dataclass
class DebugTrace:
    university: str
    discipline: str
    geo: str = ""
    country: str = ""
    events: list[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def add(self, stage: str, status: str, **details):
        self.events.append({
            "stage": stage,
            "status": status,
            "details": _safe(details),
            "at": round(time.time(), 3),
        })

    def write(self) -> Path:
        name = f"{time.strftime('%Y%m%d_%H%M%S')}_{_slug(self.university)}_{_slug(self.discipline)}.json"
        path = config.DEBUG_DIR / name
        payload = {
            "university": self.university,
            "discipline": self.discipline,
            "geo": self.geo,
            "country": self.country,
            "created_at": self.created_at,
            "events": self.events,
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return path
