# Paper Agent — Build Specification

A personal academic paper monitoring system that fetches new papers daily
from bioRxiv and Semantic Scholar, scores them for relevance using Claude
Code CLI, stores results in a local SQLite database, and serves them via
a FastAPI + static HTML interface launched on demand from the terminal.

---

## Repository Structure

```
paper-agent/
├── config/
│   ├── interests.md          # Editable interest profile (topics + labs + exclusions)
│   └── settings.yaml         # Thresholds, lookback window, source toggles
├── data/
│   ├── papers.db             # SQLite database
│   ├── staging/
│   │   ├── to_score.json     # Written by fetcher, read by scorer
│   │   └── scored.json       # Written by Claude Code, read by ingest step
│   └── logs/
│       └── run.log
├── paper_agent/
│   ├── __init__.py
│   ├── fetch.py              # Fetches from bioRxiv + Semantic Scholar
│   ├── db.py                 # Schema + read/write helpers
│   ├── ingest.py             # Reads scored.json, writes to DB
│   └── generate.py           # Renders site/index.html from DB
├── serve.py                  # Launches FastAPI + opens browser
├── run.py                    # Orchestrator: fetch → score → ingest → generate
├── add_seed.py               # Seed paper workflow (DOI or PDF path)
├── site/
│   └── index.html            # Generated static site (do not edit manually)
├── pyproject.toml
└── README.md
```

---

## Technology Stack

- **Python 3.13+** managed with `uv`
- **Dependencies:** `httpx`, `jinja2`, `fastapi`, `uvicorn`, `pyyaml`
- **`sqlite3`** from stdlib — no ORM
- **Claude Code CLI** (`claude -p`) for scoring — must be installed and
  authenticated separately by the user
- No other services required

---

## Configuration Files

### `config/interests.md`

Plain markdown, edited manually by the user. Passed directly to Claude Code
as context for scoring. Structure:

```markdown
## Research Interests

### Core topics
- 3D reconstruction and segmentation of neural tissue from electron microscopy
- Connectomics: mapping synaptic connectivity at large scale
- Network and graph-theoretic analysis of connectomes

### Methods I follow
- Machine learning and computer vision for volumetric biological images
- Statistical methods for large biological datasets
- Software tools for image analysis or connectomics pipelines

### Labs and groups I follow
- Helmstaedter lab (MPI Frankfurt)
- Lichtman lab (Harvard)
- [user should populate this list]

### Explicitly NOT interested in
- fMRI or functional neuroimaging
- Clinical or disease neuroscience unless strong methods angle
- Drug discovery or pharmacology
- Purely behavioural studies without circuit or connectivity component
```

### `config/settings.yaml`

```yaml
lookback_days: 2            # Days back to fetch — 2 handles weekend gaps

store_threshold: 5          # Papers scoring below this are discarded
                            # Tune this together with the interests prompt
                            # after reviewing initial score distributions

display_threshold: 7        # Default UI filter — shown to user on load
                            # Adjustable at runtime via the UI slider

retain_all: false           # If true, store all scored papers regardless
                            # of score. Use when calibrating the prompt
                            # or after significant changes to interests.md

biorxiv_category: neuroscience

arxiv_categories:
  - q-bio.NC
  - q-bio.QM
  - cs.CV
  - cs.LG

semantic_scholar_venues:
  - "Nature"
  - "Science"
  - "Cell"
  - "Nature Neuroscience"
  - "Nature Reviews Neuroscience"
  - "eLife"
  - "Current Biology"
  - "Neuron"
  - "Journal of Neuroscience"
  - "Journal of Neurophysiology"
  - "Science Advances"
  - "Nature Communications"

max_papers_per_source: 200  # Safety cap per fetch run
```

---

## Database Schema (`paper_agent/db.py`)

```sql
CREATE TABLE IF NOT EXISTS papers (
    doi             TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    authors         TEXT,           -- JSON array of strings
    abstract        TEXT,
    journal         TEXT,
    published_date  DATE,
    source          TEXT,           -- 'biorxiv' | 'arxiv' | 'semantic_scholar'
    url             TEXT,
    score           INTEGER,        -- 1-10
    reason          TEXT,           -- One sentence from Claude
    seen            INTEGER DEFAULT 0,
    reading_list    INTEGER DEFAULT 0,
    notes           TEXT,
    fetched_at      TEXT            -- ISO timestamp
);

CREATE INDEX IF NOT EXISTS idx_published_date ON papers(published_date);
CREATE INDEX IF NOT EXISTS idx_score ON papers(score);
CREATE INDEX IF NOT EXISTS idx_seen ON papers(seen);
```

### Helper functions

All database access goes through these functions. No raw SQL outside `db.py`.

```python
def init_db() -> None
    # Create tables and indexes if they do not exist

def get_existing_dois() -> set[str]
    # Returns all DOIs currently in the database, for deduplication

def insert_papers(papers: list[dict]) -> int
    # Batch insert, ignore conflicts on DOI. Returns count inserted.

def update_scores(scored: list[dict]) -> None
    # Updates score + reason fields by DOI

def apply_threshold(store_threshold: int, retain_all: bool) -> None
    # Deletes papers below store_threshold unless retain_all is True
    # Called after update_scores

def mark_seen(doi: str) -> None
def toggle_reading_list(doi: str) -> None
def set_note(doi: str, note: str) -> None

def get_papers_for_display(days: int = 30) -> list[dict]
    # Returns all stored papers from last N days, score descending

def get_summary() -> dict
    # Returns: {total, unread, high_score, last_fetched}
    # high_score = count of papers with score >= display_threshold
```

---

## Fetchers (`paper_agent/fetch.py`)

All fetchers return a list of dicts with keys:
`doi, title, authors, abstract, journal, published_date, source, url`

`authors` is a list of strings. `published_date` is an ISO date string
`YYYY-MM-DD`. Any paper missing a DOI or abstract is silently skipped.

### bioRxiv

```
GET https://api.biorxiv.org/details/biorxiv/{start_date}/{end_date}/{cursor}/json
```

- Paginate by incrementing cursor until `messages[0].status` indicates
  no more results
- Filter by `category == settings.biorxiv_category`
- `source = 'biorxiv'`, `journal = 'bioRxiv'`

### arXiv

```
GET https://export.arxiv.org/api/query
  ?search_query=cat:{category}
  &start=0
  &max_results=100
  &sortBy=submittedDate
  &sortOrder=descending
```

- Query once per category in `settings.arxiv_categories`
- Parse Atom XML response
- Filter to papers submitted within `lookback_days`
- arXiv IDs (e.g. `2401.12345`) should be stored as the DOI field prefixed
  `arxiv:` if no real DOI is present
- `source = 'arxiv'`, `journal = 'arXiv'`

### Semantic Scholar

```
GET https://api.semanticscholar.org/graph/v1/paper/search/bulk
  ?fields=title,authors,abstract,publicationDate,venue,externalIds,url
  &publicationDateOrYear={start_date}:{end_date}
  &venue={venue_name}
```

- Query once per venue in `settings.semantic_scholar_venues`
- Sleep 1 second between requests (rate limit: 100 req / 5 min unauthenticated)
- If env var `SEMANTIC_SCHOLAR_API_KEY` is present, pass as `x-api-key` header
- Extract DOI from `externalIds.DOI`; skip paper if absent
- `source = 'semantic_scholar'`, `journal = venue field`

### Deduplication

After all fetchers run, deduplicate the combined list:
1. Remove any paper whose DOI is already in `db.get_existing_dois()`
2. Remove duplicates within the current fetch batch (same DOI from multiple
   sources — keep the bioRxiv version as it arrives faster)

---

## Scoring Step

### Staging files

`data/staging/to_score.json` — written by `run.py` before invoking Claude:
```json
[
  {
    "doi": "10.1101/2024.01.15.123456",
    "title": "Paper title here",
    "abstract": "Abstract text here..."
  }
]
```

`data/staging/scored.json` — written by Claude Code:
```json
[
  {
    "doi": "10.1101/2024.01.15.123456",
    "score": 8,
    "reason": "Helmstaedter lab paper applying graph-theoretic analysis to dense EM reconstruction — directly relevant to your network analysis work."
  }
]
```

### Claude Code invocation

```python
import subprocess, json

def score_papers(staging_path: str = "data/staging") -> bool:
    """
    Invokes Claude Code CLI to score papers in to_score.json.
    Returns True if scored.json was successfully written, False otherwise.
    """
    prompt = f"""
Read {staging_path}/to_score.json and config/interests.md.

Score each paper's relevance to the researcher described in interests.md.
Write your output to {staging_path}/scored.json as a JSON array.

Each element must have exactly these fields:
  "doi"    — copied exactly from input
  "score"  — integer 1 to 10
  "reason" — exactly one sentence, written directly to the researcher

Scoring rules:
- Score 7+ only if you would be surprised this researcher had not seen
  this paper. A solid but unremarkable paper in their field scores 5-6.
- Score 8-10 for papers directly in their core topics or methods, or
  from a lab they explicitly follow.
- Score 1-3 for papers in their explicit exclusion list.
- The reason must be specific. "Relevant to your connectomics work" is
  too generic. "Helmstaedter lab using the graph methods you apply, on
  a new mouse cortex dataset" is good.
- A slow news day is a slow news day. Do not inflate scores to fill a
  quota. Zero papers above 7 is a valid and correct output.

Output only valid JSON. No preamble, no markdown fences, no explanation
outside the JSON array.
"""

    result = subprocess.run(
        [
            "claude", "-p",
            "--allowedTools", "Read", "Write",
            "--output-format", "json",
            prompt
        ],
        capture_output=True, text=True
    )

    scored_path = pathlib.Path(staging_path) / "scored.json"
    if not scored_path.exists():
        logging.error("Claude Code did not write scored.json")
        logging.error(result.stderr)
        return False

    try:
        json.loads(scored_path.read_text())
        return True
    except json.JSONDecodeError as e:
        logging.error(f"scored.json is malformed: {e}")
        return False
```

If scoring fails, log the error and exit cleanly without modifying the
database. Do not crash.

---

## Orchestrator (`run.py`)

```python
def main():
    settings = load_settings("config/settings.yaml")
    db.init_db()

    # 1. Fetch
    papers = fetch.fetch_all(settings)
    log(f"Fetched {len(papers)} new papers after deduplication")
    if not papers:
        log("Nothing new. Exiting.")
        return

    # 2. Stage
    write_staging(papers, "data/staging/to_score.json")

    # 3. Score
    success = score_papers()
    if not success:
        log("Scoring failed. Exiting without modifying database.")
        return

    # 4. Ingest
    scored = json.loads(Path("data/staging/scored.json").read_text())
    db.insert_papers(papers)
    db.update_scores(scored)
    db.apply_threshold(settings.store_threshold, settings.retain_all)

    kept = db.count_recent(days=1)
    high = db.count_high_score(days=1, threshold=settings.display_threshold)
    log(f"Stored {kept} papers today, {high} scored >= {settings.display_threshold}")

    # 5. Regenerate static site
    generate.build_site(settings)
    log("Site regenerated.")
```

### Cron setup

The README should include instructions for setting up a daily cron job:

```bash
# Run daily at 7am
0 7 * * * cd /path/to/paper-agent && uv run python run.py >> data/logs/run.log 2>&1
```

---

## Seed Paper Workflow (`add_seed.py`)

Allows the user to flag a paper as interesting and optionally update
`interests.md` based on it. Accepts either a DOI or a path to a PDF.

### Usage

```bash
uv run python add_seed.py --doi 10.1038/s41593-024-01234-5
uv run python add_seed.py --pdf /path/to/paper.pdf
```

### Step 1: Fetch abstract

- **DOI:** fetch abstract from Semantic Scholar by DOI
- **PDF:** write PDF path into the evaluation prompt directly — Claude Code
  can read PDFs natively via its file tools

Write result to `data/staging/seed_paper.json`:
```json
{
  "doi": "10.1038/s41593-024-01234-5",
  "title": "...",
  "abstract": "..."
}
```

### Step 2: Evaluate

Invoke Claude Code CLI with this prompt:

```
Read data/staging/seed_paper.json (or the PDF at {path} if provided).
Read config/interests.md.

The researcher has flagged this paper as interesting. Decide whether it
represents a topic, method, or angle that is meaningfully absent or
underrepresented in the current interests file.

Write your response to data/staging/seed_evaluation.json:
{
  "update_needed": true or false,
  "reasoning": "one sentence",
  "suggested_addition": "if update_needed: exact text to add and which
                         section it belongs in. Otherwise null."
}

Be conservative. If the paper is already well-covered, return false.
Small variations on existing topics do not warrant an update.
```

### Step 3: Present to user and confirm

```
Evaluation: [reasoning sentence]

Suggested addition to [Section Name]:
  "[suggested text]"

Apply? [y/n]:
```

If user confirms, invoke Claude Code again to apply the edit:

```
Read config/interests.md.
Read data/staging/seed_evaluation.json.
Apply the suggested_addition to the appropriate section of interests.md.
Do not rewrite or reorder existing content. Add only what is specified.
Write the updated file back to config/interests.md.
```

### Step 4: Log

Append to `data/logs/seed_papers.jsonl` regardless of whether the update
was applied:

```json
{"doi": "...", "title": "...", "update_needed": true, "applied": true, "timestamp": "..."}
```

---

## Static Site Generator (`paper_agent/generate.py`)

Queries the database for papers from the last 30 days. Renders
`site/index.html` using Jinja2. The output file must be fully
self-contained: all CSS and JS inline, paper data embedded as a JSON
blob in a `<script>` tag. Opening it directly in a browser (without
the FastAPI server running) must work for read-only browsing.

### Data passed to template

```python
{
  "papers": [...],          # list of paper dicts from db.get_papers_for_display()
  "summary": {...},         # from db.get_summary()
  "display_threshold": 7,   # from settings
  "generated_at": "...",    # ISO timestamp
}
```

### UI Requirements

**Overall aesthetic:** dense but readable, designed for a working researcher.
Clean typography, strong information hierarchy. Not a social media feed.
Commit to a clear design direction — dark or light — and execute it well.
Avoid generic AI-generated aesthetics.

**Header:**
- Summary line: `{unread} unread · {high_score} scored ≥{threshold} · last
  fetched {timestamp}`
- Link or button to open paper in browser (if server running) vs static notice

**Filter bar (client-side JS, no page reload):**
- Score threshold slider — default from `display_threshold` in settings
- Source filter: All / bioRxiv / arXiv / Journals
- Status filter: All / Unread / Reading list
- Text search — searches title and reason fields only (not abstract)
- Date range: 7 / 14 / 30 days

**Paper list:**
- Default sort: score descending, then date descending
- Filtered and sorted entirely in JS against the embedded JSON blob
- Each paper card shows:
  - Score — visually prominent (large number or strong colour coding)
  - Title — hyperlinked to DOI URL, opens in new tab
  - Authors — first 3 names + "et al." if more
  - Journal + published date
  - Source badge (bioRxiv / arXiv / journal name)
  - **Reason sentence** — most visually prominent text field after title.
    This is the primary value of the system.
  - Abstract — collapsed by default, expand/collapse on click
  - Action row: Mark seen · Reading list · Add note

**Action buttons:**
- Call FastAPI endpoints at `http://localhost:8000` via `fetch()`
- If the server is unreachable, grey out action buttons and show tooltip:
  "Run `papers` in terminal to enable actions"
- Notes: clicking opens an inline `<textarea>`. On blur, auto-saves via
  POST to `/set-note`. Show a subtle "saved" confirmation.

**Visual state:**
- Papers marked as seen should visually recede (reduced opacity or muted
  colours) but remain in the list
- Papers on the reading list should have a clear persistent visual marker
- Score colour coding: suggest green (8-10), amber (6-7), grey (5 and below)

---

## FastAPI Server (`serve.py`)

Minimal server — write-back actions only. The static site handles all
reads from its embedded JSON.

### Endpoints

```python
GET  /              → FileResponse("site/index.html")
POST /mark-seen     → body: {"doi": str}
POST /toggle-reading-list → body: {"doi": str}
POST /set-note      → body: {"doi": str, "note": str}
POST /regenerate    → calls generate.build_site(), returns {"status": "ok"}
```

All POST endpoints return `{"status": "ok"}` on success or
`{"status": "error", "detail": "..."}` on failure. HTTP 200 in both cases
so the client JS can handle errors gracefully without an exception.

### Launch sequence

```python
import uvicorn, webbrowser, threading, time, logging
from paper_agent.db import get_summary
from paper_agent import generate
from config_loader import load_settings

settings = load_settings("config/settings.yaml")

# Print summary before blocking on server start
summary = get_summary()
print(f"📄 {summary['unread']} unread papers "
      f"({summary['high_score']} scored ≥{settings.display_threshold}). "
      f"Opening browser...")

# Regenerate site with current DB state
generate.build_site(settings)

# Open browser after short delay to let server start
def _open():
    time.sleep(0.5)
    webbrowser.open("http://localhost:8000")

threading.Thread(target=_open, daemon=True).start()

uvicorn.run(app, host="localhost", port=8000, log_level="warning")
```

Ctrl+C kills the server cleanly. No persistent process.

### Shell alias

The README should instruct the user to add to their shell config:

```bash
alias papers="cd /path/to/paper-agent && uv run python serve.py"
```

---

## `pyproject.toml`

```toml
[project]
name = "paper-agent"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
    "httpx>=0.27",
    "jinja2>=3.1",
    "fastapi>=0.111",
    "uvicorn>=0.30",
    "pyyaml>=6.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

---

## Error Handling Principles

- **Fetch failures** (network errors, rate limits): log and continue with
  remaining sources. Partial results are better than no results.
- **Scoring failure** (Claude Code not found, malformed JSON output): log
  clearly, exit without touching the database. Do not ingest unscored papers.
- **Ingest errors** (malformed record): skip the individual record, log the
  DOI, continue with the rest.
- **Server errors** (POST endpoint fails): return error JSON, never raise
  HTTP 500 — the UI must always remain usable.
- All errors should produce clear, actionable log messages. Stack traces
  for unexpected errors, single-line summaries for expected ones.

---

## README Contents

The README should cover:
1. Prerequisites (Python 3.13+, uv, Claude Code CLI installed and authenticated)
2. Installation (`uv sync`)
3. Initial configuration (edit `config/interests.md` and `config/settings.yaml`)
4. First run: set `retain_all: true`, run `uv run python run.py`, run
   `papers`, review score distribution, tune prompt and thresholds, set
   `retain_all: false`
5. Cron setup
6. Shell alias setup
7. Seed paper workflow
8. Tuning guidance: the `store_threshold` and `display_threshold` settings
   should be calibrated together with the interests prompt. Change one at a
   time and observe the effect over a week of real runs.
