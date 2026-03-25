import json
import logging
import re
import subprocess
import threading
from datetime import datetime
from pathlib import Path

import httpx
from rich.console import Console
from rich.padding import Padding
from rich.panel import Panel

from .settings import PROJECT_ROOT

logger = logging.getLogger(__name__)

STAGING_DIR = PROJECT_ROOT / "data" / "staging"
LOG_PATH = PROJECT_ROOT / "data" / "logs" / "seed_papers.jsonl"

console = Console()


def _fetch_by_doi(doi: str) -> dict | None:
    # Try Semantic Scholar first
    try:
        resp = httpx.get(
            f"https://api.semanticscholar.org/graph/v1/paper/{doi}",
            params={"fields": "title,abstract"},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("title"):
                return {
                    "doi": doi,
                    "title": data.get("title", ""),
                    "abstract": data.get("abstract", ""),
                }
    except Exception as e:
        logger.warning(f"Semantic Scholar lookup failed for {doi}: {e}")

    # bioRxiv fallback (10.1101/ and 10.64898/ prefixes)
    if doi.startswith("10.1101/") or doi.startswith("10.64898/"):
        try:
            resp = httpx.get(
                f"https://api.biorxiv.org/details/biorxiv/{doi}/na/1",
                timeout=15,
            )
            if resp.status_code == 200:
                collection = resp.json().get("collection", [])
                if collection:
                    item = collection[0]
                    return {
                        "doi": doi,
                        "title": item.get("title", ""),
                        "abstract": item.get("abstract", ""),
                    }
        except Exception as e:
            logger.warning(f"bioRxiv lookup failed for {doi}: {e}")

    # arXiv fallback (10.48550/arXiv.XXXX.XXXXX)
    if doi.startswith("10.48550/"):
        try:
            arxiv_id = doi.split("arXiv.", 1)[-1]
            resp = httpx.get(
                f"https://export.arxiv.org/abs/{arxiv_id}",
                headers={"Accept": "application/atom+xml"},
                timeout=15,
            )
            if resp.status_code == 200:
                import xml.etree.ElementTree as ET

                ns = {"atom": "http://www.w3.org/2005/Atom"}
                root = ET.fromstring(resp.text)
                entry = root.find("atom:entry", ns)
                if entry is not None:
                    title = entry.findtext("atom:title", "", ns).strip()
                    abstract = entry.findtext("atom:summary", "", ns).strip()
                    return {"doi": doi, "title": title, "abstract": abstract}
        except Exception as e:
            logger.warning(f"arXiv lookup failed for {doi}: {e}")

    logger.error(f"Could not fetch paper for DOI: {doi}")
    return None


def _parse_stdout_object(stdout: str) -> dict | None:
    """Parse a JSON object from the --output-format json stdout envelope."""
    if not stdout:
        return None
    try:
        envelope = json.loads(stdout.strip())
        inner = envelope.get("result", "") if isinstance(envelope, dict) else ""
        if isinstance(inner, dict):
            return inner
        if isinstance(inner, str):
            inner = inner.strip()
            try:
                data = json.loads(inner)
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                pass
            m = re.search(r"\{[\s\S]*\}", inner)
            if m:
                data = json.loads(m.group(0))
                if isinstance(data, dict):
                    return data
    except Exception:
        pass
    return None


def _run_claude(cmd: list[str], timeout: int, result: dict) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    result["stdout"] = proc.stdout
    result["returncode"] = proc.returncode


def _format_suggestion(suggestion) -> str:
    if isinstance(suggestion, dict):
        section = suggestion.get("section", "")
        text = suggestion.get("text", str(suggestion))
        if section:
            return f"[bold]{section}[/bold] — {text}"
        return text
    return str(suggestion)


def _ask(prompt: str) -> str:
    console.print(f"[bold]{prompt}[/bold] ", end="")
    return input().strip().lower()


def seed(doi: str | None = None, pdf: str | None = None) -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)

    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    interests_path = PROJECT_ROOT / "config" / "interests.md"
    interests_text = interests_path.read_text()

    if doi:
        with console.status("Fetching paper…"):
            paper = _fetch_by_doi(doi)
        if not paper:
            console.print(f"[red]Could not fetch paper for DOI: {doi}[/red]")
            return
        console.print(f"[bold]{paper.get('title', doi)}[/bold]")
    elif pdf:
        paper = {"doi": pdf, "title": "(from PDF)", "abstract": ""}
        console.print(f"[bold]{pdf}[/bold]")
    else:
        console.print("[red]Provide --doi or --pdf[/red]")
        return

    # Eval: inline everything, no tool calls
    if pdf:
        paper_content = f"PDF path: {pdf}\n(abstract not available inline; see file)"
    else:
        paper_content = json.dumps(paper, indent=2)

    eval_prompt = f"""\
Here is the paper:
{paper_content}

Here is the researcher's interests profile:
{interests_text}

The researcher has flagged this paper as interesting. Do two things:

1. Score the paper 1-10 against the interests profile.
   - 8-10: directly in core topics or methods
   - 5-7: solid but unremarkable match
   - 1-4: weak or excluded
   Also give a short match_basis phrase (3-8 words) naming the strongest
   relevance reason, or null if score < 4.

2. Decide whether the paper represents a topic, method, or angle that is
   meaningfully absent or underrepresented in the current interests profile.
   Be conservative — small variations on existing topics do not warrant an update.

Output only valid JSON. No preamble, no markdown fences.
{{
  "score": integer 1-10,
  "match_basis": "short phrase or null",
  "update_needed": true or false,
  "reasoning": "one sentence",
  "suggested_addition": "if update_needed: exact text to add and which section it belongs in. Otherwise null."
}}"""

    result: dict = {}
    t = threading.Thread(
        target=_run_claude,
        args=(
            [
                "claude",
                "-p",
                "--tools",
                "",
                "--permission-mode",
                "dontAsk",
                "--output-format",
                "json",
                eval_prompt,
            ],
            120,
            result,
        ),
    )
    t.start()
    with console.status("Evaluating…"):
        t.join()

    evaluation = _parse_stdout_object(result.get("stdout", ""))
    if not evaluation:
        console.print("[red]Evaluation failed — Claude did not produce output.[/red]")
        return

    applied = False

    score = evaluation.get("score")
    match_basis = evaluation.get("match_basis")
    if score is not None:
        score_color = "green" if score >= 7 else "yellow" if score >= 5 else "red"
        basis_str = f"  [dim]{match_basis}[/dim]" if match_basis else ""
        console.print(f"\nScore: [{score_color}]{score}/10[/{score_color}]{basis_str}")

    console.print()
    console.print(
        Panel(
            evaluation.get("reasoning", ""),
            title="[bold]Evaluation[/bold]",
            border_style="dim",
        )
    )

    if evaluation.get("update_needed"):
        suggestion = evaluation.get("suggested_addition", "")
        console.print()
        console.print(Padding(_format_suggestion(suggestion), (0, 2)))
        console.print()

        if _ask("Apply? [y/n]:") == "y":
            # Apply: inline interests + suggestion, only Write needed
            suggestion_text = (
                suggestion.get("text", str(suggestion))
                if isinstance(suggestion, dict)
                else str(suggestion)
            )
            suggestion_section = (
                suggestion.get("section", "") if isinstance(suggestion, dict) else ""
            )
            section_hint = (
                f" under the '{suggestion_section}' section"
                if suggestion_section
                else ""
            )

            apply_prompt = f"""\
Here is the current contents of interests.md:
{interests_text}

Add the following line{section_hint}:
{suggestion_text}

Do not rewrite or reorder existing content. Add only this line in the appropriate place.
Write the updated file to {interests_path}."""

            result2: dict = {}
            t = threading.Thread(
                target=_run_claude,
                args=(
                    [
                        "claude",
                        "-p",
                        "--allowedTools",
                        "Write",
                        "--output-format",
                        "json",
                        apply_prompt,
                    ],
                    60,
                    result2,
                ),
            )
            t.start()
            with console.status("Applying…"):
                t.join()
            applied = True
            console.print("[green]✓[/green] Applied.")
        else:
            console.print("[dim]Skipped.[/dim]")
    else:
        console.print(
            "[dim]No update needed — paper is covered by existing interests.[/dim]"
        )

    log_entry = {
        "doi": doi or pdf,
        "title": paper.get("title", ""),
        "update_needed": evaluation.get("update_needed", False),
        "applied": applied,
        "timestamp": datetime.now().isoformat(),
    }
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(log_entry) + "\n")
