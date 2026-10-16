from loguru import logger
from rich.console import Console
from rich.progress import track

from .models import Result
from .pipeline import run_university
from .store import Store

console = Console()  # shared with main.py so log lines print above the progress bar cleanly


def run_items(run_id: str, items: list[dict], retry_failed: bool = False) -> list[Result]:
    """Shared by Batch and GEO modes. Safe to interrupt (Ctrl+C) and re-run: finished rows are skipped."""
    store = Store()
    store.add_items(run_id, items)
    todo = store.pending(run_id, retry_failed)
    console.print(f"Run [bold]{run_id}[/bold]: {len(items)} items, {len(todo)} to process")
    for it in track(todo, description="Processing", console=console, transient=True):
        res = run_university(it["university"], it["discipline"], it["geo"], it["country"], it["website"])
        store.save(run_id, it["key"], res)
    return store.results(run_id)