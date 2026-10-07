I# sieve

Literature Sieve — automated paper monitoring for neuroscience/connectomics research.

## Development Environment

```bash
uv sync
uv run ruff check src/
uv run ruff format src/
```

### Key Commands

| Command | Description |
|---------|-------------|
| `sieve run` | Fetch → score → ingest → generate static site |
| `sieve serve` | Start FastAPI server + open browser |
| `sieve seed --doi <DOI>` | Evaluate a paper and optionally update interests |
| `sieve cite --doi <DOI>` | Fetch and score a citation graph |
| `sieve clean` | Prune low-score papers outside fetch window |
| `sieve health` | Per-source fetch health table and open issues |
| `sieve export --from FILE` | Generate standalone annotated bibliography HTML |
| `poe lab` | Launch Jupyter Lab |

## Architecture & Key Files

```
config/
  settings.yaml       # Thresholds, sources, batch size
  interests.md        # Research interest profile for scoring
src/sieve/
  settings.py         # Settings dataclass + load_settings()
  db.py               # SQLite schema + all DB helpers
  fetch.py            # bioRxiv API + arXiv RSS + journal RSS fetchers
  score.py            # Batched scoring via `claude -p` CLI
  ingest.py           # Atomic DB ingest (papers + scores in one tx)
  generate.py         # Jinja2 → site/index.html
  server.py           # FastAPI write-back endpoints
  cli.py              # Entry point: main() with subcommands run/serve/seed/cite/clean
  seed.py             # Seed paper evaluation workflow
  health.py           # Source/pipeline health checks from run history
  templates/
    index.html.j2     # Self-contained HTML/CSS/JS template
data/                 # gitignored — papers.db, staging/, logs/
site/                 # gitignored — generated index.html
```

Pipeline: `fetch_all()` → `score_papers()` → `ingest()` → `build_site()`

Scoring invokes `claude -p` with batches of ~30 papers. Papers + scores are inserted atomically (no orphaned unscored records).

## Data & External Dependencies

- **bioRxiv REST API** — paginated, filtered by category
- **arXiv RSS** — `https://rss.arxiv.org/rss/{category}` (latest day only)
- **Journal RSS feeds** — configured in settings.yaml; feeds with `issn:` instead of `url:` are fetched from the CrossRef API (Cell Press RSS is behind a Cloudflare challenge)
- **Semantic Scholar API** — optional abstract enrichment for RSS entries
- **Claude Code CLI** — must be installed and authenticated (`claude -p`)
- **SQLite** — `data/papers.db` (stdlib sqlite3, no ORM)

## Conventions & Patterns

- All DB access through `db.py` helper functions
- Atomic ingest: `insert_papers_with_scores()` handles both in one transaction
- Per-batch error handling in scoring: if one batch fails, others still run; Claude calls retry transient errors, and a logged-out CLI aborts the run (`ClaudeAuthError`)
- Papers fetched but not scored go to `data/pending.json` and are retried on later runs (up to 5 attempts)
- Every `sieve run` records per-source results (`source_runs`) and a run summary (`runs`) in papers.db. `health.py` compares each source to its own history (fetch errors, empty feeds, staleness vs usual gap, abstract coverage drops, model changes, overdue runs). Results show in the site header, at the end of `sieve run`, and in `sieve health`; new problems trigger a macOS notification (single fetch blips do not)
- All commands log at INFO to `data/logs/sieve.log`; the terminal shows warnings only
- Server returns `{status: ok/error}` with HTTP 200 for all POSTs
- Static site works read-only without server; actions need `sieve serve`
