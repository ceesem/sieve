import json
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

from . import db
from .cite import fetch_citation_graph
from .fetch import fetch_all, save_pending
from .generate import build_bibliography, build_site
from .ingest import ingest_batch
from .score import ClaudeAuthError, score_papers
from .seed import learn as learn_interests
from .seed import seed as seed_paper
from .settings import PROJECT_ROOT, load_settings

LOG_PATH = PROJECT_ROOT / "data" / "logs" / "sieve.log"
_LOG_FORMAT = logging.Formatter(
    "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)
# Terminal output; quieted to WARNING while rich progress bars are showing.
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_LOG_FORMAT)

logger = logging.getLogger(__name__)
# File-only logger for run markers, so they don't clutter the terminal.
_file_logger = logging.getLogger("sieve.logfile")
_file_logger.propagate = False


def _setup_logging() -> None:
    """Log to the terminal and, at INFO, to data/logs/sieve.log for every
    invocation, so manual and scheduled runs leave the same record."""
    from logging.handlers import RotatingFileHandler

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        LOG_PATH, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(_LOG_FORMAT)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(_console_handler)
    root.addHandler(file_handler)
    _file_logger.addHandler(file_handler)
    _file_logger.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _quiet_console() -> None:
    """Keep INFO chatter off the terminal (it still goes to sieve.log)."""
    _console_handler.setLevel(logging.WARNING)


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
        "Bootstrap interests from a single paper (cold start)",
        "--doi DOI [--pdf PATH]",
    )
    table.add_row(
        "learn",
        "Tune interests from your reading list + negative examples",
        "[--min-examples N]",
    )
    table.add_row(
        "cite",
        "Fetch and score a citation graph around a paper",
        "--doi DOI [--forward] [--recommend]",
    )
    table.add_row("clean", "Prune low-score papers outside the fetch window", "")
    table.add_row("health", "Show per-source fetch health and open issues", "")
    table.add_row(
        "export",
        "Generate a standalone annotated bibliography",
        "--from FILE [--output PATH] [--title TEXT] [--interests PATH]",
    )

    console.print(Padding(table, (0, 0, 0, 2)))
    console.print()
    console.print(
        Text("Usage: ", style="bold") + Text("sieve <command> [options]", style="")
    )
    console.print()


def _score_ingest_build(papers, settings, progress, console, stats=None, build=True):
    """Shared score → ingest → build-site pipeline with rich progress tasks.

    `stats` is passed through to score_papers (failed batches, models used).
    """
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

    scored_dois: set[str] = set()
    try:
        for batch_papers, batch_scores in score_papers(
            papers,
            settings,
            haiku_callback=_on_haiku,
            sonnet_callback=_on_sonnet,
            sonnet_start_callback=_on_sonnet_start,
            stats=stats,
        ):
            total_inserted += ingest_batch(batch_papers, batch_scores, run_timestamp)
            scored_dois.update(batch_scores)
            total_high += sum(
                1
                for s in batch_scores.values()
                if (s.get("score") or 0) >= settings.display_threshold
            )
    finally:
        save_pending([p for p in papers if p["doi"] not in scored_dois])

    progress.update(haiku_task, description="[green]✓[/green] Haiku triage")
    progress.update(
        sonnet_task, description="[green]✓[/green] Sonnet scoring", visible=True
    )

    if build:
        site_task = progress.add_task("Building site…", total=1)
        build_site(settings)
        progress.update(
            site_task, completed=1, description="[green]✓[/green] Site built"
        )

    return total_inserted, total_high


def _trigger() -> str:
    """ "scheduled" when launched by the com.sieve.* launchd job."""
    return (
        "scheduled"
        if os.environ.get("XPC_SERVICE_NAME", "").startswith("com.sieve")
        else "manual"
    )


def _finish_run(run_info: dict, report: dict, settings, console) -> None:
    """Record the run, report health, notify on new problems, rebuild the site."""
    from rich.text import Text

    from .health import compute_health

    try:
        db.record_run(run_info, report)
        health = compute_health(settings)
        run_info["issues"] = [i.key for i in health.problems]
        db.record_run(run_info, report)

        runs = db.get_runs(limit=2)
        previous = set(runs[1]["issues"]) if len(runs) > 1 else set()
        new = [i for i in health.problems if i.key not in previous and i.notify]

        logger.info(f"Health: {health.summary()}")
        for issue in health.issues:
            logger.log(
                logging.INFO if issue.severity == "info" else logging.WARNING,
                f"Health {issue.severity}: {issue.message}",
            )

        style = {"ok": "green", "info": "dim", "warn": "yellow", "error": "red"}
        console.print(Text(f"\nHealth: {health.summary()}", style=style[health.level]))
        for issue in health.issues:
            marker = "  new " if issue in new else "      "
            console.print(Text(marker + issue.message, style=style[issue.severity]))
        if health.problems:
            console.print("[dim]  Details: sieve health[/dim]")

        if new:
            more = f" (+{len(new) - 1} more)" if len(new) > 1 else ""
            _notify("sieve: source health", new[0].message + more)
    except Exception as e:  # health reporting must never break a run
        logger.error(f"Health check failed: {e}")

    build_site(settings)


def run(args=None) -> None:
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
    _quiet_console()

    console = Console()
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    )

    run_info = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "trigger": _trigger(),
        "fetched": 0,
        "ingested": 0,
        "failed_batches": 0,
        "models": set(),
        "error": None,
    }
    report: dict = {}
    stats: dict = {}
    total_inserted = total_high = 0

    try:
        if not _wait_for_network(console):
            run_info["error"] = "no network"
            console.print("[red]No network — skipping this run.[/red]")
            sys.exit(1)

        with progress:
            fetch_task = progress.add_task("Fetching papers…", total=None)
            try:
                papers = fetch_all(settings, report)
            except (KeyboardInterrupt, Exception) as exc:
                progress.stop()
                if isinstance(exc, KeyboardInterrupt):
                    run_info["error"] = "interrupted"
                    console.print("[yellow]Interrupted.[/yellow]")
                else:
                    run_info["error"] = f"fetch failed: {exc}"
                    console.print(f"[red]Fetch failed:[/red] {exc}")
                return
            run_info["fetched"] = len(papers)
            progress.update(
                fetch_task,
                total=1,
                completed=1,
                description=f"[green]✓[/green] Fetched {len(papers)} new papers",
            )

            if papers:
                total_inserted, total_high = _score_ingest_build(
                    papers, settings, progress, console, stats=stats, build=False
                )
                run_info["ingested"] = total_inserted
    except ClaudeAuthError as e:
        run_info["error"] = f"Claude CLI not authenticated: {e}"
        raise
    finally:
        run_info["failed_batches"] = stats.get("failed_batches", 0)
        run_info["models"] = stats.get("models", set())
        if run_info["error"] is None:
            console.print(
                f"\nProcessed [bold]{total_inserted}[/bold] papers · "
                f"[bold cyan]{total_high}[/bold cyan] above threshold (≥{settings.display_threshold})"
            )
            logger.info(
                f"Run complete: {run_info['fetched']} fetched, {total_inserted} ingested, "
                f"{total_high} scored ≥{settings.display_threshold}"
            )
        _finish_run(run_info, report, settings, console)


def health(args=None) -> None:
    """Print per-source health and any open issues."""
    from rich.console import Console
    from rich.table import Table

    from .health import compute_health

    settings = load_settings()
    db.init_db()
    h = compute_health(settings)
    console = Console()

    colour = {"ok": "green", "warn": "yellow", "error": "red", "unknown": "dim"}
    table = Table(box=None, pad_edge=False, header_style="bold")
    for col in (
        "Source",
        "Status",
        "Entries",
        "New 7d",
        "Last new",
        "Usual gap",
        "Abstracts 7d",
    ):
        table.add_column(
            col,
            justify="left" if col in ("Source", "Status", "Last new") else "right",
            no_wrap=col == "Source",
        )
    for sh in h.sources:
        table.add_row(
            sh.name,
            f"[{colour[sh.status]}]{sh.status}[/]",
            "–" if sh.last_entries is None else str(sh.last_entries),
            str(sh.new_7d),
            str(sh.last_new or "–"),
            "–" if sh.typical_gap is None else f"{sh.typical_gap:.0f}d",
            "–" if sh.abstract_pct is None else f"{sh.abstract_pct:.0%}",
        )
    console.print()
    console.print(table)
    console.print()

    if h.last_run:
        r = h.last_run
        models = ", ".join(r["models"]) or "–"
        console.print(
            f"[bold]Last run[/bold] {r['run_at']} ({r['trigger']}): "
            f"{r['fetched']} fetched, {r['ingested']} ingested, "
            f"{r['failed_batches']} failed batches · models: {models}"
        )
    style = {"error": "red", "warn": "yellow", "info": "dim"}
    console.print(f"[bold]{h.summary()}[/bold]")
    for issue in h.issues:
        console.print(
            f"  [{style[issue.severity]}]{issue.severity:5}[/] {issue.message}"
        )
    console.print()


def _notify(title: str, message: str) -> None:
    """Best-effort macOS notification — `sieve run` usually runs unattended."""
    if sys.platform != "darwin":
        return
    script = (
        f"display notification {json.dumps(message)} with title {json.dumps(title)}"
    )
    try:
        subprocess.run(["osascript", "-e", script], timeout=10, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass


def _wait_for_network(console, attempts: int = 10, delay: float = 30) -> bool:
    """Wait for DNS/network after wake from sleep, when launchd fires the job."""
    import httpx

    for i in range(attempts):
        try:
            httpx.head("https://api.biorxiv.org", timeout=10)
            return True
        except httpx.HTTPError:
            if i == 0:
                console.print("[yellow]Network unavailable — waiting…[/yellow]")
            time.sleep(delay)
    return False


def _find_free_port(start: int = 8000, end: int = 8020) -> int:
    import socket

    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free port found in range {start}–{end}")


def serve(args=None) -> None:
    import uvicorn

    settings = load_settings()
    db.init_db()

    from rich.console import Console

    console = Console()

    deleted = db.prune_papers(settings.site_threshold, settings.lookback_days)
    if deleted:
        console.print(
            f"Pruned [bold]{deleted}[/bold] low-score papers outside the fetch window."
        )

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

    port = _find_free_port()
    if port != 8000:
        console.print(f"[yellow]Port 8000 in use — using port {port}[/yellow]")

    def _open():
        time.sleep(0.5)
        webbrowser.open(f"http://localhost:{port}")

    threading.Thread(target=_open, daemon=True).start()

    from .server import app

    server = uvicorn.Server(
        uvicorn.Config(app, host="localhost", port=port, log_level="warning")
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
    _quiet_console()

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
    settings = load_settings()
    seed_paper(doi=args.doi, pdf=getattr(args, "pdf", None), settings=settings)


def learn(args) -> None:
    db.init_db()
    settings = load_settings()
    learn_interests(
        min_examples=args.min_examples,
        recent_k=None if args.all else args.recent,
        older_sample=args.older_sample,
        settings=settings,
    )


def _parse_doi_file(path: str, ignore_errors: bool) -> list[str]:
    """Parse a DOI list from a plain-text or BibTeX file.

    Plain text: one DOI per line; blank lines and # comments ignored.
    BibTeX (.bib): extracts doi fields; enforces that all entries have one.
    Strips https://doi.org/ prefixes. Raises SystemExit on user abort.
    """
    import re

    from rich.console import Console

    console = Console()
    text = open(path).read()

    if path.endswith(".bib"):
        # Find all named entries (exclude @string/@preamble/@comment)
        entry_pattern = re.compile(
            r"@(?!string|preamble|comment)(\w+)\s*\{\s*([^,]+),",
            re.IGNORECASE,
        )
        doi_pattern = re.compile(
            r"\bdoi\s*=\s*[{\"](.*?)[}\"]", re.IGNORECASE | re.DOTALL
        )

        entries = list(entry_pattern.finditer(text))
        dois: list[str] = []
        missing: list[tuple[str, str]] = []

        for m in entries:
            entry_type = m.group(1)
            entry_key = m.group(2).strip()
            # Slice the entry body: from match start to next @ or end of file
            start = m.start()
            next_at = text.find("\n@", start + 1)
            body = text[start:next_at] if next_at != -1 else text[start:]
            doi_match = doi_pattern.search(body)
            if doi_match:
                raw = doi_match.group(1).strip()
                raw = re.sub(r"^https?://doi\.org/", "", raw, flags=re.IGNORECASE)
                dois.append(raw)
            else:
                missing.append((entry_key, f"@{entry_type}"))

        if missing and not ignore_errors:
            console.print(
                f"\n[yellow]{len(missing)} entr{'y' if len(missing) == 1 else 'ies'} "
                f"{'is' if len(missing) == 1 else 'are'} missing a DOI:[/yellow]"
            )
            for key, etype in missing:
                console.print(f"  [dim]{key}[/dim] ({etype})")
            console.print()
            answer = input("Continue anyway? [y/N] ").strip().lower()
            if answer != "y":
                raise SystemExit(1)

        return dois

    # Plain text
    dois = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        import re as _re

        line = _re.sub(r"^https?://doi\.org/", "", line, flags=_re.IGNORECASE)
        dois.append(line)
    return dois


def export(args) -> None:
    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, TextColumn

    console = Console()

    dois = _parse_doi_file(args.from_file, args.ignore_errors)
    if not dois:
        console.print("[yellow]No DOIs found — nothing to export.[/yellow]")
        return

    output_path = Path(args.output)
    interests_path = Path(args.interests) if args.interests else None

    if interests_path:
        settings = load_settings()
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            progress.add_task("Annotating with custom interests…", total=None)
            found, missing = build_bibliography(
                dois, output_path, args.title, interests_path, settings=settings
            )
    else:
        found, missing = build_bibliography(dois, output_path, args.title)

    console.print(
        f"Generated [bold]{found}[/bold] paper{'s' if found != 1 else ''} → [cyan]{output_path}[/cyan]"
    )
    if missing:
        console.print(
            f"[yellow]{len(missing)} DOI{'s' if len(missing) != 1 else ''} not found in DB:[/yellow]"
        )
        for doi in missing:
            console.print(f"  [dim]{doi}[/dim]")


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

    # learn
    p_learn = subparsers.add_parser(
        "learn", help="Tune interests from reading list + negative examples"
    )
    p_learn.add_argument(
        "--min-examples",
        type=int,
        default=3,
        metavar="N",
        help="Minimum flagged examples required before proposing edits (default: 3)",
    )
    p_learn.add_argument(
        "--recent",
        type=int,
        default=50,
        metavar="K",
        help="Most-recently-saved reading-list papers to always include (default: 50)",
    )
    p_learn.add_argument(
        "--older-sample",
        type=int,
        default=25,
        metavar="M",
        help="Random sample of older reading-list papers to add (default: 25)",
    )
    p_learn.add_argument(
        "--all",
        action="store_true",
        default=False,
        help="Use the entire reading list, ignoring recency sampling",
    )

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
    subparsers.add_parser("health", help="Show source and pipeline health")

    # export
    p_export = subparsers.add_parser(
        "export", help="Generate a standalone annotated bibliography"
    )
    p_export.add_argument(
        "--from",
        dest="from_file",
        required=True,
        metavar="FILE",
        help="DOI source: plain text (one per line) or .bib file",
    )
    p_export.add_argument(
        "--output",
        default="site/bibliography.html",
        metavar="PATH",
        help="Output HTML path (default: site/bibliography.html)",
    )
    p_export.add_argument(
        "--title",
        default="Annotated Bibliography",
        metavar="TEXT",
        help="Page title",
    )
    p_export.add_argument(
        "--interests",
        default=None,
        metavar="PATH",
        help="Custom interests.md for Sonnet re-annotation",
    )
    p_export.add_argument(
        "--ignore-errors",
        action="store_true",
        default=False,
        help="Skip BibTeX entries missing a DOI without prompting",
    )

    args = parser.parse_args()
    _setup_logging()
    if args.command:
        _file_logger.info(f"=== sieve {args.command} ({_trigger()}) ===")

    if args.command is None or args.help:
        _print_help()
        return

    dispatch = {
        "run": run,
        "serve": serve,
        "seed": seed,
        "learn": learn,
        "cite": cite,
        "clean": clean,
        "health": health,
        "export": export,
    }
    try:
        dispatch[args.command](args)
    except ClaudeAuthError as e:
        from rich.console import Console

        msg = f"Claude CLI is not authenticated ({e}). Fetched papers are queued for the next run."
        Console().print(f"\n[red]{msg}[/red]")
        logger.error(msg)
        if args.command != "run":  # run reports this through its health check
            _notify(
                "sieve: Claude login needed",
                "Run `claude` and /login, then `sieve run`.",
            )
        sys.exit(1)
