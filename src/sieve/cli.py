import logging
import sys
import threading
import time
import webbrowser
from datetime import datetime

from . import db
from .cite import fetch_citation_graph
from .fetch import fetch_all
from .generate import build_site
from .ingest import ingest_batch
from .score import score_papers
from .seed import seed as seed_paper
from .settings import load_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _score_ingest_build(papers, settings, progress, console):
    """Shared score → ingest → build-site pipeline with rich progress tasks."""
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
    )

    n_haiku = (len(papers) + settings.batch_size - 1) // settings.batch_size
    haiku_task = progress.add_task("Haiku triage…", total=n_haiku)
    sonnet_task = progress.add_task("Sonnet scoring…", total=None, visible=False)

    def _on_haiku(done, total):
        progress.update(haiku_task, completed=done, total=total)

    def _on_sonnet_start(total):
        progress.update(sonnet_task, total=total, completed=0, visible=True)

    def _on_sonnet(done, total):
        progress.update(sonnet_task, completed=done, total=total)

    run_timestamp = datetime.now().isoformat(timespec="seconds")
    total_inserted = 0
    total_high = 0

    for batch_papers, batch_scores in score_papers(
        papers,
        settings,
        haiku_callback=_on_haiku,
        sonnet_callback=_on_sonnet,
        sonnet_start_callback=_on_sonnet_start,
    ):
        total_inserted += ingest_batch(batch_papers, batch_scores, run_timestamp)
        total_high += sum(
            1
            for s in batch_scores.values()
            if (s.get("score") or 0) >= settings.display_threshold
        )

    progress.update(haiku_task, description="[green]✓[/green] Haiku triage")
    progress.update(
        sonnet_task, description="[green]✓[/green] Sonnet scoring", visible=True
    )

    site_task = progress.add_task("Building site…", total=1)
    build_site(settings)
    progress.update(site_task, completed=1, description="[green]✓[/green] Site built")

    return total_inserted, total_high


def run() -> None:
    import argparse
    import logging as _logging

    from rich.console import Console
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
    )

    parser = argparse.ArgumentParser()
    parser.add_argument("--site-threshold", type=int, default=None)
    args, _ = parser.parse_known_args()

    settings = load_settings()
    if args.site_threshold is not None:
        settings.site_threshold = args.site_threshold
    db.init_db()

    logger.info("sieve-run started")
    _logging.getLogger("sieve").setLevel(_logging.WARNING)
    _logging.getLogger("httpx").setLevel(_logging.WARNING)

    console = Console()
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    )

    with progress:
        fetch_task = progress.add_task("Fetching papers…", total=None)
        papers = fetch_all(settings)
        progress.update(
            fetch_task,
            total=1,
            completed=1,
            description=f"[green]✓[/green] Fetched {len(papers)} new papers",
        )

        if not papers:
            site_task = progress.add_task("Rebuilding site…", total=1)
            build_site(settings)
            progress.update(
                site_task, completed=1, description="[green]✓[/green] Site rebuilt"
            )
            return

        total_inserted, total_high = _score_ingest_build(
            papers, settings, progress, console
        )

    console.print(
        f"\nStored [bold]{total_inserted}[/bold] papers · "
        f"[bold cyan]{total_high}[/bold cyan] scored ≥ {settings.display_threshold}"
    )


def serve() -> None:
    import uvicorn

    settings = load_settings()
    db.init_db()

    summary = db.get_summary(
        settings.display_threshold,
        days=settings.lookback_days,
        site_threshold=settings.site_threshold,
    )
    print(
        f"{summary['unread']} unread papers "
        f"({summary['high_score']} scored >={settings.display_threshold}). "
        f"Opening browser..."
    )

    build_site(settings)

    def _open():
        time.sleep(0.5)
        webbrowser.open("http://localhost:8000")

    threading.Thread(target=_open, daemon=True).start()

    from .server import app

    server = uvicorn.Server(
        uvicorn.Config(app, host="localhost", port=8000, log_level="warning")
    )

    def _quit_on_enter():
        input("\nPress Enter to quit sieve-serve...\n")
        server.should_exit = True

    threading.Thread(target=_quit_on_enter, daemon=True).start()
    server.run()


def clean() -> None:
    settings = load_settings()
    db.init_db()
    deleted = db.prune_papers(settings.site_threshold, settings.lookback_days)
    logger.info(f"Pruned {deleted} low-score papers outside the fetch window.")


def cite_cli() -> None:
    import argparse
    import logging as _logging

    from rich.console import Console
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
    )

    parser = argparse.ArgumentParser()
    parser.add_argument("--doi", required=True)
    parser.add_argument("--forward", action="store_true", default=False)
    parser.add_argument("--recommend", action="store_true", default=False)
    parser.add_argument("--site-threshold", type=int, default=None)
    args, _ = parser.parse_known_args()

    settings = load_settings()
    if args.site_threshold is not None:
        settings.site_threshold = args.site_threshold
    db.init_db()

    # Suppress info-level logs so they don't interleave with rich output
    _logging.getLogger("sieve").setLevel(_logging.WARNING)
    _logging.getLogger("httpx").setLevel(_logging.WARNING)

    console = Console()
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    )

    with progress:
        # --- Fetch ---
        fetch_task = progress.add_task("Fetching citation graph…", total=None)
        papers = fetch_citation_graph(
            args.doi,
            forward=args.forward,
            recommend=args.recommend,
            mailto=settings.mailto,
        )
        progress.update(
            fetch_task,
            total=1,
            completed=1,
            description=f"[green]✓[/green] Fetched {len(papers)} new papers",
        )

        if not papers:
            site_task = progress.add_task("Rebuilding site…", total=1)
            build_site(settings)
            progress.update(
                site_task, completed=1, description="[green]✓[/green] Site rebuilt"
            )
            return

        total_inserted, total_high = _score_ingest_build(
            papers, settings, progress, console
        )

    console.print(
        f"\nStored [bold]{total_inserted}[/bold] papers · "
        f"[bold cyan]{total_high}[/bold cyan] scored ≥ {settings.display_threshold}"
    )


def seed_cli() -> None:
    doi = None
    pdf = None
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--doi" and i + 1 < len(args):
            doi = args[i + 1]
            i += 2
        elif args[i] == "--pdf" and i + 1 < len(args):
            pdf = args[i + 1]
            i += 2
        else:
            i += 1

    db.init_db()
    seed_paper(doi=doi, pdf=pdf)
