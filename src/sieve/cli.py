import logging
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


def _print_help():
    from rich.console import Console
    from rich.padding import Padding
    from rich.table import Table
    from rich.text import Text

    console = Console()
    console.print()
    console.print(
        Text("sieve", style="bold cyan")
        + Text(" — literature monitoring for neuroscience/connectomics", style="dim")
    )
    console.print()

    table = Table(box=None, pad_edge=False, show_header=False, padding=(0, 2, 0, 0))
    table.add_column(style="bold green", no_wrap=True)
    table.add_column(style="")
    table.add_column(style="dim")

    table.add_row(
        "run",
        "Fetch, score, ingest, and rebuild the static site",
        "[--site-threshold N]",
    )
    table.add_row("serve", "Build site and start local server with write-back", "")
    table.add_row(
        "seed",
        "Evaluate a paper and optionally update interests",
        "--doi DOI [--pdf PATH]",
    )
    table.add_row(
        "cite",
        "Fetch and score a citation graph around a paper",
        "--doi DOI [--forward] [--recommend]",
    )
    table.add_row("clean", "Prune low-score papers outside the fetch window", "")

    console.print(Padding(table, (0, 0, 0, 2)))
    console.print()
    console.print(
        Text("Usage: ", style="bold") + Text("sieve <command> [options]", style="")
    )
    console.print()


def _score_ingest_build(papers, settings, progress, console):
    """Shared score → ingest → build-site pipeline with rich progress tasks."""
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


def run(args=None) -> None:
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

    settings = load_settings()
    if args is not None and args.site_threshold is not None:
        settings.site_threshold = args.site_threshold
    db.init_db()

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
        f"\nProcessed [bold]{total_inserted}[/bold] papers · "
        f"[bold cyan]{total_high}[/bold cyan] above threshold (≥{settings.display_threshold})"
    )


def serve(args=None) -> None:
    import uvicorn

    settings = load_settings()
    db.init_db()

    from rich.console import Console

    console = Console()

    summary = db.get_summary(
        settings.display_threshold,
        days=settings.lookback_days,
        site_threshold=settings.site_threshold,
    )
    console.print(
        f"[bold]{summary['unread']}[/bold] unread papers "
        f"([cyan]{summary['high_score']}[/cyan] scored ≥{settings.display_threshold}) · "
        f"opening browser…"
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
        input("\nPress Enter to quit…\n")
        server.should_exit = True

    threading.Thread(target=_quit_on_enter, daemon=True).start()
    server.run()


def clean(args=None) -> None:
    from rich.console import Console

    console = Console()

    settings = load_settings()
    db.init_db()
    deleted = db.prune_papers(settings.site_threshold, settings.lookback_days)
    console.print(
        f"Pruned [bold]{deleted}[/bold] low-score papers outside the fetch window."
    )


def cite(args) -> None:
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

    settings = load_settings()
    if args.site_threshold is not None:
        settings.site_threshold = args.site_threshold
    db.init_db()

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
        f"\nProcessed [bold]{total_inserted}[/bold] papers · "
        f"[bold cyan]{total_high}[/bold cyan] above threshold (≥{settings.display_threshold})"
    )


def seed(args) -> None:
    db.init_db()
    seed_paper(doi=args.doi, pdf=getattr(args, "pdf", None))


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="sieve", add_help=False)
    parser.add_argument("-h", "--help", action="store_true")
    subparsers = parser.add_subparsers(dest="command")

    # run
    p_run = subparsers.add_parser("run", help="Fetch, score, ingest, rebuild site")
    p_run.add_argument(
        "--site-threshold",
        type=int,
        default=None,
        metavar="N",
        help="Override site display threshold",
    )

    # serve
    subparsers.add_parser("serve", help="Start local server and open browser")

    # seed
    p_seed = subparsers.add_parser(
        "seed", help="Evaluate a paper, optionally update interests"
    )
    p_seed.add_argument("--doi", default=None, metavar="DOI")
    p_seed.add_argument("--pdf", default=None, metavar="PATH")

    # cite
    p_cite = subparsers.add_parser("cite", help="Fetch and score a citation graph")
    p_cite.add_argument("--doi", required=True, metavar="DOI")
    p_cite.add_argument(
        "--forward",
        action="store_true",
        default=False,
        help="Include forward citations",
    )
    p_cite.add_argument(
        "--recommend",
        action="store_true",
        default=False,
        help="Include Semantic Scholar recommendations",
    )
    p_cite.add_argument("--site-threshold", type=int, default=None, metavar="N")

    # clean
    subparsers.add_parser("clean", help="Prune low-score papers outside fetch window")

    args = parser.parse_args()

    if args.command is None or args.help:
        _print_help()
        return

    dispatch = {
        "run": run,
        "serve": serve,
        "seed": seed,
        "cite": cite,
        "clean": clean,
    }
    dispatch[args.command](args)
