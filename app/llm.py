"""Single wrapper for every Qwen call: thinking off, JSON-schema output, resident model, small context."""
import time

import ollama
from loguru import logger
from pydantic import BaseModel, ValidationError

from . import config

_client = None
_available: bool | None = None


def _get_client():
    global _client
    if _client is None:
        _client = ollama.Client(timeout=config.OLLAMA_TIMEOUT)
    return _client


def available(refresh: bool = False) -> bool:
    global _available
    if _available is not None and not refresh:
        return _available
    try:
        models = _get_client().list().models
        names = {(getattr(m, "model", None) or m["name"]) for m in models}
    except Exception as e:
        logger.warning(f"Ollama not reachable: {e}")
        _available = False
        return False
    _available = config.OLLAMA_MODEL in names
    if not _available:
        logger.warning(f"Model {config.OLLAMA_MODEL} not found. Run: ollama pull {config.OLLAMA_MODEL}")
    return _available


def _chat(prompt: str, schema: type[BaseModel], system: str = ""):
    messages = [
        {"role": "system", "content": f"{system}\nRespond with JSON only. /no_think".strip()},
        {"role": "user", "content": f"{prompt}\n/no_think"},
    ]
    kwargs = dict(
        model=config.OLLAMA_MODEL,
        messages=messages,
        format=schema.model_json_schema(),
        options={"temperature": 0, "num_ctx": config.OLLAMA_NUM_CTX},
        keep_alive=config.OLLAMA_KEEP_ALIVE,
    )
    try:
        return _get_client().chat(think=False, **kwargs)
    except TypeError:  # older ollama client without `think`
        return _get_client().chat(**kwargs)


def ask(prompt: str, schema: type[BaseModel], system: str = "") -> BaseModel | None:
    """Return a validated schema instance, or None if the LLM is unavailable/fails."""
    if not available():
        return None
    try:
        resp = _chat(prompt, schema, system)
        return schema.model_validate_json(resp["message"]["content"])
    except (ValidationError, Exception) as e:
        logger.warning(f"LLM call failed: {e}")
        return None


def benchmark(runs: int = 5) -> list[dict]:
    """Cold vs warm latency on three representative prompts."""

    class Pick(BaseModel):
        index: int

    class Out(BaseModel):
        name: str = ""
        role: str = ""
        email: str = ""

    prompts = [
        ("dept-pick", Pick,
         "Pick the department page for 'Computer Science'. Reply with its index, or -1.\n"
         "0. https://x.edu/admissions/btech-cs | B.Tech CS admissions\n"
         "1. https://x.edu/departments/computer-science | Department of Computer Science\n"
         "2. https://x.edu/news/cs-lab | New CS lab opens"),
        ("page-classify", Pick,
         "Which link is the faculty directory? Reply with its index, or -1.\n"
         "0. /about | About us\n1. /people/faculty | Our Faculty\n2. /contact | Contact"),
        ("contact-extract", Out,
         "Extract the head of department. Copy exactly.\nDr. Meera Iyer\nProfessor & Head of Department\n"
         "meera.iyer@cs.x.edu\n\nDr. Sam Roy\nAssistant Professor\nsam.roy@cs.x.edu"),
    ]
    rows = []
    try:  # force a cold start
        _get_client().generate(model=config.OLLAMA_MODEL, prompt="", keep_alive=0)
        time.sleep(1)
    except Exception:
        pass
    for label, schema, prompt in prompts:
        for i in range(runs if label == "dept-pick" else 2):
            t0 = time.time()
            resp = _chat(prompt, schema)
            wall = time.time() - t0
            g = lambda k: (getattr(resp, k, 0) or 0) / 1e9
            n = getattr(resp, "eval_count", 0) or 0
            rows.append({
                "prompt": label,
                "kind": "cold" if (label == "dept-pick" and i == 0) else "warm",
                "wall_s": round(wall, 2), "load_s": round(g("load_duration"), 2),
                "prompt_eval_s": round(g("prompt_eval_duration"), 2),
                "eval_s": round(g("eval_duration"), 2),
                "tok_per_s": round(n / g("eval_duration"), 1) if g("eval_duration") else 0,
            })
    return rows