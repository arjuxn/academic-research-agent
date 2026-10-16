# Academic Research Agent — Desktop UI

A local-first academic contact discovery tool. It uses public institutional pages, deterministic extraction, and optional local Ollama/Qwen3 4B fallback. No paid API or cloud LLM is required.

## Modes
1. **Single College** — university, discipline, campus/location hints, optional official website.
2. **Discipline + GEO** — discover universities for a discipline and region, then process them.
3. **Batch CSV** — process a CSV with `University Name`, `GEO`, `Country/Region`, `Discipline`, and optional `Website`.

Results are written to `output/` as Excel and CSV. Multi-institution runs use SQLite state in `data/state.db`, so pending/error rows can resume. The UI includes a log, progress bar, result table, and output-folder shortcut.

## Windows setup
1. Install Python 3.11+ (Python 3.13 should work if all dependencies provide wheels).
2. Double-click `setup_windows.bat` and wait for dependencies plus Chromium to install.
3. Ensure Ollama is installed and running, then run `ollama pull qwen3:4b`.
4. Double-click `run_ui.bat`.

If you already have a virtual environment and dependencies, you can run `python ui.py` from the project folder.

## CLI remains available
Examples:
```powershell
python main.py single "Bharati Vidyapeeth" "Computer Science and Engineering" --campus Pune --country India --show-candidates
python main.py geo "Computer Science" "Maharashtra" --country India --limit 10
python main.py batch input/universities.csv
```

## Notes
- The crawler only reads public pages. It does not bypass CAPTCHAs or access controls.
- Bot-protection pages that return HTTP 200 are now detected in department-page validation and reported as blocked evidence.
- `Department Page Blocked` means a candidate URL was found but access was blocked/unavailable; it does not mean the underlying contact is absent.
- The sample CSV is only an example. Replace its row with your intended inputs.
