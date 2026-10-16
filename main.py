import csv
import hashlib
import re
from pathlib import Path

import typer
from loguru import logger
from rich.table import Table

from app import config, llm
from app.discover import discover_universities
from app.export import export
from app.models import Result
from app.pipeline import run_university
from app.runner import console, run_items

logger.remove()
logger.add(lambda m: console.print(m, end="", markup=False, highlight=False),
           level="INFO", format="{time:HH:mm:ss} {message}\n")
logger.add(config.DATA_DIR / "run.log", level="DEBUG", rotation="5 MB")

app = typer.Typer(
    add_completion=False,
    invoke_without_command=True,
    help="University department-head contact finder (local-first, public-web crawler).",
)


def show(results: list[Result]):
    t = Table(show_lines=False)
    for c in ("University", "Department", "Contact", "Role", "Email", "Method", "Status"):
        t.add_column(c, overflow="fold")
    for r in results:
        t.add_row(r.university_name, r.target_department, r.contact_name, r.contact_type,
                  r.email, r.extraction_method, r.status)
    console.print(t)


def finish(results: list[Result], name: str):
    show(results)
    xlsx, csv_path = export(results, name)
    console.print(f"\nExcel: [green]{xlsx}[/green]\nCSV:   {csv_path}")
    for r in results:
        if r.debug_file:
            console.print(f"Debug: {r.debug_file}")


def load_batch_items(csv_path: Path, discipline: str = "") -> list[dict]:
    raw = csv_path.read_bytes()
    items = []
    for row in csv.DictReader(raw.decode("utf-8-sig").splitlines()):
        name = (row.get("University Name") or "").strip()
        disc = (row.get("Discipline") or discipline).strip()
        if not name or not disc:
            continue
        items.append({"university": name, "discipline": disc, "geo": (row.get("GEO") or "").strip(),
                      "country": (row.get("Country/Region") or "").strip(),
                      "website": (row.get("Website") or "").strip()})
    return items


def interactive_menu():
    console.print("\n[bold]Faculty Contact Finder[/bold]")
    console.print("1. Single college")
    console.print("2. Discipline + geo")
    console.print("3. Batch CSV")
    console.print("4. LLM health check")
    choice = typer.prompt("Choose a mode", default="1").strip()
    if choice == "1":
        university = typer.prompt("College/university name").strip()
        discipline = typer.prompt("Discipline").strip()
        geo = typer.prompt("GEO/region", default="").strip()
        country = typer.prompt("Country/Region", default="").strip()
        website = typer.prompt("Official website if known", default="").strip()
        finish([run_university(university, discipline, geo, country, website)], "single")
    elif choice == "2":
        discipline = typer.prompt("Discipline").strip()
        region = typer.prompt("Region/GEO").strip()
        country = typer.prompt("Country/Region", default="").strip()
        limit = int(typer.prompt("Limit", default="20"))
        unis = discover_universities(discipline, region, limit)
        items = [{"university": u["name"], "discipline": discipline, "geo": region, "country": country,
                  "website": u["website"]} for u in unis]
        slug = re.sub(r"\W+", "-", f"{discipline}-{region}".lower()).strip("-")
        finish(run_items(f"geo-{slug}", items), f"geo_{slug}")
    elif choice == "3":
        csv_path = Path(typer.prompt("CSV path", default="input/universities.csv"))
        discipline = typer.prompt("Default discipline for rows without one", default="").strip()
        raw = csv_path.read_bytes()
        items = load_batch_items(csv_path, discipline)
        if not items:
            console.print("[red]No usable rows (need University Name and a Discipline).[/red]")
            raise typer.Exit(1)
        run_id = "batch-" + hashlib.sha1(raw).hexdigest()[:10]
        finish(run_items(run_id, items), "batch")
    elif choice == "4":
        check_llm()
    else:
        console.print("[red]Unknown mode.[/red]")
        raise typer.Exit(1)


@app.callback()
def main(ctx: typer.Context):
    """Run without a subcommand to open the interactive menu."""
    if ctx.invoked_subcommand is None:
        interactive_menu()
        raise typer.Exit()


@app.command()
def single(university: str, discipline: str,
           country: str = typer.Option("", "--country"),
           geo: str = typer.Option("", "--geo"),
           campus: str = typer.Option("", "--campus", help="Campus name or location for multi-campus disambiguation"),
           website: str = typer.Option("", "--website", help="Skip Stage 1 by giving the official site"),
           show_candidates: bool = typer.Option(False, "--show-candidates", help="Show website and department candidates before selection")):
    """Mode 1: one university + discipline."""
    res = run_university(university, discipline, geo=geo, country=country, website=website,
                         campus=campus, show_candidates=show_candidates)
    finish([res], "single")


@app.command()
def geo(discipline: str, region: str,
        country: str = typer.Option("", "--country"),
        limit: int = typer.Option(20, "--limit"),
        retry_failed: bool = typer.Option(False, "--retry-failed")):
    """Mode 2: discover universities in a region, then run the pipeline on each."""
    unis = discover_universities(discipline, region, limit)
    if not unis:
        console.print("[red]No universities discovered.[/red]")
        raise typer.Exit(1)
    console.print(f"Discovered {len(unis)} universities")
    items = [{"university": u["name"], "discipline": discipline, "geo": region, "country": country,
              "website": u["website"]} for u in unis]
    slug = re.sub(r"\W+", "-", f"{discipline}-{region}".lower()).strip("-")
    finish(run_items(f"geo-{slug}", items, retry_failed), f"geo_{slug}")


@app.command()
def batch(csv_path: Path = typer.Argument(Path("input/universities.csv")),
          discipline: str = typer.Option("", "--discipline", help="Default for rows without a Discipline"),
          retry_failed: bool = typer.Option(False, "--retry-failed", help="Also retry Not Found rows")):
    """Mode 3: CSV with University Name, GEO, Country/Region, Discipline (+ optional Website)."""
    raw = csv_path.read_bytes()
    items = load_batch_items(csv_path, discipline)
    if not items:
        console.print("[red]No usable rows (need University Name and a Discipline).[/red]")
        raise typer.Exit(1)
    run_id = "batch-" + hashlib.sha1(raw).hexdigest()[:10]  # same file content -> resumes same run
    finish(run_items(run_id, items, retry_failed), "batch")


@app.command("check-llm")
def check_llm():
    """Is Ollama running and the Qwen model pulled?"""
    ok = llm.available(refresh=True)
    console.print("[green]LLM ready[/green]" if ok else f"[red]Not ready.[/red] Run: ollama pull {config.OLLAMA_MODEL}")


@app.command("bench-llm")
def bench_llm():
    """Cold vs warm latency of Qwen on this machine."""
    if not llm.available(refresh=True):
        console.print("[red]LLM not available.[/red]")
        raise typer.Exit(1)
    t = Table(title=f"{config.OLLAMA_MODEL} latency (seconds)")
    cols = ("prompt", "kind", "wall_s", "load_s", "prompt_eval_s", "eval_s", "tok_per_s")
    for c in cols:
        t.add_column(c)
    for r in llm.benchmark():
        t.add_row(*[str(r[c]) for c in cols])
    console.print(t)


if __name__ == "__main__":
    app()
