# Academic Research Agent

A local-first academic contact discovery tool that finds university department pages and extracts publicly available contact details for senior academic staff, prioritizing the Head of Department.

It uses deterministic web extraction with an optional local Ollama model (Qwen3 4B) for selected extraction tasks. No paid APIs or cloud LLMs are required.

## Features

- **Single College:** Find a department and its senior contact at one institution.
- **Discipline + GEO:** Discover universities for a discipline and region, then process them.
- **Batch CSV:** Process multiple universities from a CSV file.
- **Contact extraction:** Extract names, roles, emails, phone numbers, and profile links.
- **Multiple sources:** Search HTML pages, faculty directories, profile pages, and PDFs.
- **Resume support:** SQLite tracks multi-institution runs so unfinished work can resume.
- **Exports:** Save results to Excel and CSV, with debug logs for troubleshooting.
- **Desktop UI and CLI:** Use the graphical interface or terminal commands.

## Tech Stack

- **Language:** Python
- **Desktop UI:** PySide6
- **Web requests and parsing:** Requests, BeautifulSoup, lxml
- **Search and matching:** DuckDuckGo Search, RapidFuzz
- **Browser rendering:** Playwright
- **Data and exports:** SQLite, Pandas, OpenPyXL
- **PDF extraction:** PyMuPDF, pdfplumber
- **Local LLM:** Ollama with Qwen3 4B
- **CLI:** Typer and Rich

## Architecture

The application follows a staged pipeline:

1. **Website discovery:** Find candidate official university websites.
2. **Department discovery:** Identify the relevant department page.
3. **Faculty discovery:** Find faculty directories and relevant profile pages.
4. **Contact extraction:** Extract names, roles, and contact details.
5. **Contact selection:** Prioritize senior leadership roles using available evidence.
6. **Storage and export:** Store progress in SQLite and export results to Excel and CSV.

Deterministic extraction is preferred. The local LLM is an optional fallback for selected tasks.

## Windows Setup

### 1. Install prerequisites

Install Python 3.11 or 3.12 and [Ollama](https://ollama.com/download).

Python 3.13 may work, provided all dependencies support your environment.

### 2. Open the project folder

Open PowerShell or a terminal in the directory containing `main.py`, `ui.py`, and `requirements.txt`.

### 3. Create a virtual environment

```powershell
python -m venv .venv
```

Activate it:

```powershell
.\.venv\Scripts\Activate.ps1
```

### 4. Install dependencies

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 5. Install the browser

```powershell
python -m playwright install chromium
```

### 6. Set up the local model

Pull Qwen3 4B:

```powershell
ollama pull qwen3:4b
```

Ensure Ollama is running, then verify the model:

```powershell
python main.py check-llm
```

### 7. Launch the desktop UI

```powershell
python ui.py
```

Alternatively, run the CLI:

```powershell
python main.py
```

## CLI Usage

### Single College

```powershell
python main.py single "Bharati Vidyapeeth" "Computer Science and Engineering" --geo "Pune" --country "India"
```

If you know the official website, provide it to reduce website-discovery work:

```powershell
python main.py single "Bharati Vidyapeeth" "Computer Science and Engineering" --website "https://www.bvuniversity.edu.in"
```

### Discipline + GEO

```powershell
python main.py geo "Computer Science" "Maharashtra" --country "India" --limit 10
```

### Batch CSV

Create a CSV with these headers:

```csv
University Name,GEO,Country/Region,Discipline,Website
Bharati Vidyapeeth,Pune,India,Computer Science and Engineering,https://www.bvuniversity.edu.in
IIT Delhi,Delhi,India,Computer Science and Engineering,
```

Run it:

```powershell
python main.py batch input/universities.csv
```

If some rows have no discipline, supply a default:

```powershell
python main.py batch input/universities.csv --discipline "Computer Science"
```

## Output and Resume

- **Excel and CSV:** Saved in `output/`.
- **SQLite state:** Stored in `data/state.db`.
- **Logs and debug traces:** Check the project's data and log directories when a run fails.

Multi-institution runs can resume unfinished work using the same run inputs and stored state. Use `--retry-failed` when you also want to retry previously not-found rows.

## Troubleshooting

**Playwright reports a missing browser**

```powershell
python -m playwright install chromium
```

**Ollama is unavailable**

```powershell
ollama list
python main.py check-llm
```

Confirm that `qwen3:4b` is installed and Ollama is running.

**Department discovery fails**

Inspect the logs and debug output. Try providing the official university website or checking whether the relevant department page is accessible.

**Department Page Blocked**

A candidate department URL was found, but the content could not be accessed or verified because of a block or availability issue. This does not mean the department or its contacts do not exist.

## Responsible Crawling

The scraper uses publicly accessible institutional pages. It does not bypass CAPTCHAs, authentication, or access controls. Automated access may be restricted by some websites, so results should be checked against official sources before use.