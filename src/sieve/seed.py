import json
import logging
import re
import shutil
import subprocess
import threading
from datetime import datetime

import httpx
from rich.console import Console
from rich.padding import Padding
from rich.panel import Panel

from . import db
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


def seed(
    doi: str | None = None, pdf: str | None = None, downgrade: bool = False
) -> None:
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

    if downgrade:
        eval_prompt = f"""\
Here is the paper:
{paper_content}

Here is the researcher's interests profile:
{interests_text}

The researcher has flagged this paper as scoring too HIGH — they want papers like this \
to rank lower in future.

1. Score the paper 1-10 against the current interests profile.
   - 8-10: directly in core topics or methods
   - 5-7: solid but unremarkable match
   - 1-4: weak or excluded

2. Suggest specific text to add to the interests profile that would cause papers like \
this to score lower. This might be an explicit exclusion ("not if purely about X"), a \
qualifier on an existing interest ("only when applied to Y, not Z"), or a new \
low-priority note. Be precise and minimal — do not rewrite existing content. If the \
paper is already a weak match or no addition would help, set suggested_addition to null.

Output only valid JSON. No preamble, no markdown fences.
{{
  "score": integer 1-10,
  "reasoning": "one sentence explaining why this paper currently scores high",
  "update_needed": true or false,
  "suggested_addition": "if update_needed: exact text to add and which section it belongs in. Otherwise null."
}}"""
    else:
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
    if score is not None:
        score_color = "green" if score >= 7 else "yellow" if score >= 5 else "red"
        if downgrade:
            console.print(f"\nCurrent score: [{score_color}]{score}/10[/{score_color}]")
        else:
            match_basis = evaluation.get("match_basis")
            basis_str = f"  [dim]{match_basis}[/dim]" if match_basis else ""
            console.print(
                f"\nScore: [{score_color}]{score}/10[/{score_color}]{basis_str}"
            )

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


# ---------------------------------------------------------------------------
# learn — tune interests.md from accumulated database signals
# ---------------------------------------------------------------------------

LEARN_LOG_PATH = PROJECT_ROOT / "data" / "logs" / "learn.jsonl"
INTERESTS_BACKUP_DIR = PROJECT_ROOT / "data" / "backups"


def _format_examples(papers: list[dict]) -> str:
    """Signal-dense rendering of example papers for the learn prompt.

    Includes the full abstract — it is the ground truth about what the paper is
    about, and the strongest signal for inferring recurring interests. The
    sampling cap (see db.get_positive_examples) keeps the total bounded.
    """
    lines = []
    for p in papers:
        parts = [f"- {p.get('title') or '(untitled)'}"]
        meta = []
        if p.get("journal"):
            meta.append(p["journal"])
        if p.get("score") is not None:
            meta.append(f"score {p['score']}")
        if p.get("match_basis"):
            meta.append(f"matched: {p['match_basis']}")
        if meta:
            parts.append(f"  ({'; '.join(meta)})")
        if p.get("reason"):
            parts.append(f"  why it matched: {p['reason']}")
        if p.get("abstract"):
            parts.append(f"  abstract: {p['abstract']}")
        lines.append("\n".join(parts))
    return "\n".join(lines)


def _build_learn_prompt(
    interests_text: str, positives: list[dict], negatives: list[dict]
) -> str:
    pos_block = _format_examples(positives) or "(none)"
    neg_block = _format_examples(negatives) or "(none)"
    return f"""\
You are tuning a researcher's interests profile (interests.md). An LLM uses this
profile to score incoming papers for relevance. You are given the current profile
and two sets of examples drawn from the researcher's own behavior:

  - SAVED PAPERS — added to their reading list. These are "more like this".
  - NEGATIVE EXAMPLES — explicitly flagged "less like this".

Propose minimal, surgical edits to interests.md that would make the scorer better
reflect these signals. You may ADD new lines, REVISE existing lines (to sharpen
vague wording, make a line more specific, or merge redundant ones), and REMOVE
lines — but removal is the most aggressive action; use it sparingly.

Bias strongly toward RECALL over precision. The scorer is a permissive triage
filter, and missing a relevant paper is worse than admitting a borderline one.
Therefore:
- ADD liberally: new lines that broaden or sharpen what counts as relevant,
  grounded in recurring themes across the saved papers. A new narrow exclusion
  is also an addition (phrase it as "...only when purely about X").
- REVISE for clarity: rewrite a vague or overly broad existing line to be more
  specific and useful, or merge two redundant lines into one. When a revision
  NARROWS scope, be conservative — only when negatives show a clear pattern.
- REMOVE conservatively: only a line that is clearly stale (no longer reflected
  in any saved paper), fully redundant with another line, or persistently driving
  false positives. When in doubt, prefer a revision, or propose nothing. Never
  remove a still-relevant interest just to tidy up.
- For REVISE and REMOVE, copy the "before"/"text" field VERBATIM from the current
  interests.md (exact characters) so the edit can be applied precisely.
- Do not restate anything already covered by the profile.
- A handful of weak examples may warrant zero edits. That is a valid result.

Do not ask for permission. Do not mention tools. Return only the final JSON object.

Current interests.md:
{interests_text}

SAVED PAPERS (more like this):
{pos_block}

NEGATIVE EXAMPLES (less like this):
{neg_block}

Output only valid JSON, no preamble or markdown fences:
{{
  "summary": "1-3 sentences naming the patterns you found",
  "additions": [{{"section": "section name", "text": "exact line to add"}}],
  "revisions": [{{"section": "section name", "before": "exact existing line",
                 "after": "rewritten line", "rationale": "why"}}],
  "removals": [{{"section": "section name", "text": "exact existing line",
                "rationale": "why it is safe to remove"}}]
}}"""


def _section(e: dict) -> str:
    s = e.get("section", "")
    return f"[dim]{s}[/dim] — " if s else ""


def _render_additions(edits: list[dict]) -> None:
    for e in edits:
        console.print(
            Padding(f"[green]+[/green] {_section(e)}{e.get('text', '')}", (0, 2))
        )


def _render_revisions(edits: list[dict]) -> None:
    for e in edits:
        rationale = e.get("rationale", "")
        console.print(Padding(f"[cyan]~[/cyan] {_section(e)}", (0, 2)))
        console.print(Padding(f"[red]- {e.get('before', '')}[/red]", (0, 4)))
        console.print(Padding(f"[green]+ {e.get('after', '')}[/green]", (0, 4)))
        if rationale:
            console.print(Padding(f"[dim]{rationale}[/dim]", (0, 4)))


def _render_removals(edits: list[dict]) -> None:
    for e in edits:
        rationale = e.get("rationale", "")
        rat = f"  [dim]({rationale})[/dim]" if rationale else ""
        console.print(
            Padding(f"[red]-[/red] {_section(e)}{e.get('text', '')}{rat}", (0, 2))
        )


def _backup_interests(interests_path):
    """Copy interests.md to a timestamped backup before a destructive edit."""
    INTERESTS_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    dest = INTERESTS_BACKUP_DIR / f"interests-{stamp}.md"
    shutil.copy2(interests_path, dest)
    return dest


def learn(
    min_examples: int = 3, recent_k: int | None = 50, older_sample: int = 25
) -> None:
    """Propose interests.md edits from accumulated reading-list / negative signals.

    recent_k / older_sample tune the graded recency sample of reading-list
    positives (see db.get_positive_examples); recent_k=None uses every saved paper.
    """
    logging.getLogger("httpx").setLevel(logging.WARNING)
    LEARN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    interests_path = PROJECT_ROOT / "config" / "interests.md"
    interests_text = interests_path.read_text()

    positives = db.get_positive_examples(recent_k=recent_k, older_sample=older_sample)
    negatives = db.get_negative_examples()
    total = len(positives) + len(negatives)

    console.print(
        f"Learning from [bold]{len(positives)}[/bold] reading-list paper(s) and "
        f"[bold]{len(negatives)}[/bold] negative example(s)."
    )
    if total < min_examples:
        console.print(
            f"[yellow]Too few examples ({total} < {min_examples}) to learn from "
            f"reliably.[/yellow] Browse and flag more papers, or bootstrap with "
            f"[bold]sieve seed --doi DOI[/bold]."
        )
        return

    prompt = _build_learn_prompt(interests_text, positives, negatives)
    result: dict = {}
    t = threading.Thread(
        target=_run_claude,
        args=(
            [
                "claude",
                "-p",
                "--model",
                "claude-sonnet-4-6",
                "--tools",
                "",
                "--permission-mode",
                "dontAsk",
                "--output-format",
                "json",
                prompt,
            ],
            300,
            result,
        ),
    )
    t.start()
    with console.status("Analyzing your saved and rejected papers…"):
        t.join()

    proposal = _parse_stdout_object(result.get("stdout", ""))
    if not proposal:
        console.print("[red]Learning failed — Claude did not produce output.[/red]")
        return

    additions = proposal.get("additions") or []
    revisions = proposal.get("revisions") or []
    removals = proposal.get("removals") or []

    console.print()
    console.print(
        Panel(
            proposal.get("summary", "(no summary)"),
            title="[bold]What I found[/bold]",
            border_style="dim",
        )
    )

    if not (additions or revisions or removals):
        console.print(
            "\n[dim]No edits proposed — your interests already cover these signals.[/dim]"
        )
        _log_learn(positives, negatives, proposal, {})
        return

    # Per-bucket review and confirmation: additions are safe, revisions rewrite a
    # line in place, removals delete one — so each is confirmed separately, letting
    # you take the safe edits while declining a risky rewrite or deletion.
    apply_add, apply_rev, apply_rem = [], [], []
    if additions:
        console.print("\n[bold green]Add[/bold green]  [dim](new lines)[/dim]")
        _render_additions(additions)
        if _ask(f"\nApply {len(additions)} addition(s)? [y/n]:") == "y":
            apply_add = additions
    if revisions:
        console.print("\n[bold cyan]Revise[/bold cyan]  [dim](rewrite in place)[/dim]")
        _render_revisions(revisions)
        if _ask(f"\nApply {len(revisions)} revision(s)? [y/n]:") == "y":
            apply_rev = revisions
    if removals:
        console.print("\n[bold red]Remove[/bold red]  [dim](delete lines)[/dim]")
        _render_removals(removals)
        if _ask(f"\nApply {len(removals)} removal(s)? [y/n]:") == "y":
            apply_rem = removals

    if not (apply_add or apply_rev or apply_rem):
        console.print("\n[dim]Nothing applied.[/dim]")
        _log_learn(positives, negatives, proposal, {})
        return

    # Back up interests.md before any in-place rewrite or deletion.
    backup = None
    if apply_rev or apply_rem:
        backup = _backup_interests(interests_path)
        console.print(f"[dim]Backed up interests.md → {backup}[/dim]")

    sections = [
        "Apply exactly the edits below and nothing else. Keep every other "
        "line — its wording, ordering, and formatting — byte-for-byte identical."
    ]
    if apply_add:
        lines = [f"- [{e.get('section', '')}] {e.get('text', '')}" for e in apply_add]
        sections.append(
            "[ADD] Insert each new line under its named section:\n" + "\n".join(lines)
        )
    if apply_rev:
        blocks = [
            f"- [{e.get('section', '')}]\n  before: {e.get('before', '')}\n"
            f"  after:  {e.get('after', '')}"
            for e in apply_rev
        ]
        sections.append(
            "[REVISE] Replace each before-line with its after-line, "
            "matching the before text exactly:\n" + "\n".join(blocks)
        )
    if apply_rem:
        lines = [f"- [{e.get('section', '')}] {e.get('text', '')}" for e in apply_rem]
        sections.append(
            "[REMOVE] Delete each line entirely (and tidy any leftover "
            "blank line):\n" + "\n".join(lines)
        )

    apply_prompt = (
        f"Here is the current contents of interests.md:\n{interests_text}\n\n"
        + "\n\n".join(sections)
        + f"\n\nWrite the updated file to {interests_path}."
    )

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
    console.print(
        f"[green]✓[/green] Applied {len(apply_add)} addition(s), "
        f"{len(apply_rev)} revision(s), {len(apply_rem)} removal(s)."
    )
    if backup:
        console.print(f"[dim]Restore with: cp {backup} {interests_path}[/dim]")

    _log_learn(
        positives,
        negatives,
        proposal,
        {
            "additions": len(apply_add),
            "revisions": len(apply_rev),
            "removals": len(apply_rem),
        },
        backup=str(backup) if backup else None,
    )


def _log_learn(
    positives: list[dict],
    negatives: list[dict],
    proposal: dict,
    applied: dict,
    backup: str | None = None,
) -> None:
    entry = {
        "n_positive": len(positives),
        "n_negative": len(negatives),
        "proposed": {
            "additions": len(proposal.get("additions") or []),
            "revisions": len(proposal.get("revisions") or []),
            "removals": len(proposal.get("removals") or []),
        },
        "applied": applied,
        "backup": backup,
        "timestamp": datetime.now().isoformat(),
    }
    with open(LEARN_LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")
