import json
import logging
import subprocess
from datetime import datetime
from pathlib import Path

import httpx

from .settings import PROJECT_ROOT

logger = logging.getLogger(__name__)

STAGING_DIR = PROJECT_ROOT / "data" / "staging"
LOG_PATH = PROJECT_ROOT / "data" / "logs" / "seed_papers.jsonl"


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


def seed(doi: str | None = None, pdf: str | None = None) -> None:
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    interests_path = PROJECT_ROOT / "config" / "interests.md"

    # Step 1: Get paper info
    if doi:
        paper = _fetch_by_doi(doi)
        if not paper:
            print(f"Could not fetch paper for DOI: {doi}")
            return
        seed_path = STAGING_DIR / "seed_paper.json"
        seed_path.write_text(json.dumps(paper, indent=2))
        paper_ref = f"Read {seed_path}."
    elif pdf:
        paper_ref = f"Read the PDF at {pdf}."
        paper = {"doi": pdf, "title": "(from PDF)"}
    else:
        print("Provide --doi or --pdf")
        return

    # Step 2: Evaluate
    eval_path = STAGING_DIR / "seed_evaluation.json"
    if eval_path.exists():
        eval_path.unlink()

    eval_prompt = f"""\
{paper_ref}
Read {interests_path}.

The researcher has flagged this paper as interesting. Decide whether it
represents a topic, method, or angle that is meaningfully absent or
underrepresented in the current interests file.

Write your response to {eval_path} as JSON:
{{
  "update_needed": true or false,
  "reasoning": "one sentence",
  "suggested_addition": "if update_needed: exact text to add and which section it belongs in. Otherwise null."
}}

Be conservative. If the paper is already well-covered, return false.
Small variations on existing topics do not warrant an update."""

    subprocess.run(
        [
            "claude",
            "-p",
            "--allowedTools",
            "Read",
            "Write",
            "--output-format",
            "json",
            eval_prompt,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    if not eval_path.exists():
        print("Evaluation failed — Claude did not produce output.")
        return

    evaluation = json.loads(eval_path.read_text())
    applied = False

    # Step 3: Present and confirm
    print(f"\nEvaluation: {evaluation.get('reasoning', '')}")

    if evaluation.get("update_needed"):
        print(f'\nSuggested addition:\n  "{evaluation.get("suggested_addition", "")}"')
        confirm = input("\nApply? [y/n]: ").strip().lower()

        if confirm == "y":
            apply_prompt = f"""\
Read {interests_path}.
Read {eval_path}.
Apply the suggested_addition to the appropriate section of interests.md.
Do not rewrite or reorder existing content. Add only what is specified.
Write the updated file back to {interests_path}."""

            subprocess.run(
                [
                    "claude",
                    "-p",
                    "--allowedTools",
                    "Read",
                    "Write",
                    "--output-format",
                    "json",
                    apply_prompt,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            applied = True
            print("Applied.")
        else:
            print("Skipped.")
    else:
        print("No update needed — paper is covered by existing interests.")

    # Step 4: Log
    log_entry = {
        "doi": doi or pdf,
        "title": paper.get("title", ""),
        "update_needed": evaluation.get("update_needed", False),
        "applied": applied,
        "timestamp": datetime.now().isoformat(),
    }
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(log_entry) + "\n")
