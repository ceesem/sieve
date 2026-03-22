import hashlib
import json
import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .settings import PROJECT_ROOT, Settings

logger = logging.getLogger(__name__)

STAGING_DIR = PROJECT_ROOT / "data" / "staging"


def _build_haiku_prompt(interests_text: str, batch_data: list[dict]) -> str:
    return f"""\
Score each paper's relevance to the researcher described below.
This is a permissive triage pass for a firehose: the goal is to avoid
missing plausibly relevant papers, even if some false positives survive.

Each element must have exactly these fields:
  "doi"         — copied exactly from input
  "score"       — integer 1 to 10
  "match_basis" — for scores >= 4, a specific 3-8 word phrase naming the
                  single strongest relevance basis from interests.md;
                  for scores below 4, null

Rules for "match_basis":
- Use a short, specific phrase grounded in interests.md.
- Prefer the strongest single basis, not a list.
- Avoid generic phrases like "relevant to your interests",
  "neuroscience methods", or "cell type study".
- Good examples: "connectomics graph analysis",
  "interneuron cortical circuits", "EM segmentation methods",
  "cell type connectivity".
- If the paper is only weakly relevant, the phrase should still name the
  main reason it cleared the threshold.

Scoring rules:
- Score 7+ only if you would be surprised this researcher had not seen
  this paper. A solid but unremarkable paper in their field scores 5-6.
- Score 8-10 for papers directly in their core topics or methods, or
  from a lab they explicitly follow.
- Score 1-3 for papers in their explicit exclusion list.
- When uncertain between two adjacent scores, prefer the higher score if
  the paper plausibly matches an important interest and is worth manual review.
- It is acceptable for this pre-filter to include some false positives.
- A slow news day is a slow news day. Do not inflate scores to fill a
  quota. Zero papers above 7 is a valid and correct output.
- The title and abstract should dominate.
- Papers from labs known to do strong work in relevant areas may receive
  a modest upward adjustment, but should not outweigh weak content.

Do not ask for permission. Do not mention tools. Do not propose file,
shell, bash, or python operations. Return only the final JSON array.

Researcher profile (from interests.md):
{interests_text}

Papers to score:
{json.dumps(batch_data, indent=2)}

Output only valid JSON. No preamble, no markdown fences, no explanation
outside the JSON array."""


def _build_sonnet_prompt(interests_text: str, batch_data: list[dict]) -> str:
    return f"""\
These papers passed a coarse Haiku pre-filter. Re-evaluate each one
carefully against the researcher's interests and assign a refined score.
This is the higher-precision pass: decide how strong the match really is
and explain it clearly to the researcher.

The input JSON includes a "match_basis" field — Haiku's preliminary hypothesis
for why the paper matched. Use it as a starting hypothesis, not as ground
truth; refine or correct it if needed.

Each element must have exactly these fields:
  "doi"    — copied exactly from input
  "score"  — integer 1 to 10, same scale as the Haiku scoring rules
  "reason" — exactly one sentence, written directly to the researcher

Scoring rules (same as pre-filter):
- Score 7+ only if you would be surprised this researcher had not seen
  this paper. A solid but unremarkable paper in their field scores 5-6.
- Score 8-10 for papers directly in their core topics or methods, or
  from a lab they explicitly follow.
- Score 1-3 for papers in their explicit exclusion list.
- A slow news day is a slow news day. Do not inflate scores.
- Be more selective than the Haiku pre-filter when the evidence is weak
  or the match is indirect.

The reason must be specific. "Relevant to your connectomics work" is
too generic. "Helmstaedter lab using the graph methods you apply, on
a new mouse cortex dataset" is good.

Do not ask for permission. Do not mention tools. Do not propose file,
shell, bash, or python operations. Return only the final JSON array.

Researcher profile (from interests.md):
{interests_text}

Papers to score:
{json.dumps(batch_data, indent=2)}

Output only valid JSON. No preamble, no markdown fences, no explanation
outside the JSON array."""


def _parse_stdout_result(stdout: str, batch_idx: int, stage: str) -> list | None:
    """Parse a JSON array from the --output-format json stdout envelope."""
    if not stdout:
        return None
    try:
        envelope = json.loads(stdout.strip())
        inner = envelope.get("result", "") if isinstance(envelope, dict) else ""
        if isinstance(inner, list):
            logger.info(
                f"Batch {batch_idx} ({stage}): parsed {len(inner)} items from stdout"
            )
            return inner
        if isinstance(inner, dict):
            return None
        inner = inner.strip()

        # 1. Entire result is a JSON array
        try:
            data = json.loads(inner)
            if isinstance(data, list):
                logger.info(
                    f"Batch {batch_idx} ({stage}): parsed {len(data)} items from stdout"
                )
                return data
        except json.JSONDecodeError:
            pass

        # 2. JSON array inside a markdown fence anywhere in the text
        import re as _re

        fence_match = _re.search(r"```(?:json)?\s*(\[.*?\])\s*```", inner, _re.DOTALL)
        if fence_match:
            data = json.loads(fence_match.group(1))
            if isinstance(data, list):
                logger.info(
                    f"Batch {batch_idx} ({stage}): parsed {len(data)} items from stdout"
                )
                return data

        # 3. Bare JSON array anywhere in the text
        array_match = _re.search(r"(\[[\s\S]*\])", inner)
        if array_match:
            data = json.loads(array_match.group(1))
            if isinstance(data, list):
                logger.info(
                    f"Batch {batch_idx} ({stage}): parsed {len(data)} items from stdout"
                )
                return data

    except (json.JSONDecodeError, Exception):
        pass
    return None


def _run_claude(
    model: str,
    prompt: str,
    output_path: Path,
    batch_idx: int,
    stage: str,
) -> list | None:
    """Run claude CLI and return parsed JSON from stdout, or None on failure."""
    try:
        result = subprocess.run(
            [
                "claude",
                "-p",
                "--model",
                model,
                "--tools",
                "",
                "--permission-mode",
                "dontAsk",
                "--output-format",
                "json",
                prompt,
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )

        parsed = _parse_stdout_result(result.stdout, batch_idx, stage)
        if parsed is not None:
            output_path.write_text(json.dumps(parsed))
            return parsed

        logger.error(f"Batch {batch_idx} ({stage}): Claude returned invalid JSON")
        if result.returncode != 0:
            logger.error(f"returncode: {result.returncode}")
        if result.stderr:
            logger.error(
                f"stderr: {result.stderr[:500] if result.stderr else '(empty)'}"
            )
        if result.stdout:
            logger.error(
                f"stdout: {result.stdout[:500] if result.stdout else '(empty)'}"
            )
        return None

    except subprocess.TimeoutExpired:
        logger.error(f"Batch {batch_idx} ({stage}): Claude CLI timed out")
        return None
    except (json.JSONDecodeError, Exception) as e:
        logger.error(f"Batch {batch_idx} ({stage}): error — {e}")
        return None


def score_papers(
    papers: list[dict],
    settings: Settings,
    haiku_callback=None,
    sonnet_callback=None,
    sonnet_start_callback=None,
):
    """Score papers in batches using two-stage Haiku+Sonnet approach.

    Stage 1: Haiku scores all papers (1-10, brief rationale).
    Stage 2: Sonnet writes reasons only for papers >= store_threshold.

    Yields (batch_papers, batch_scores) per batch where batch_scores is
    {doi: {score, reason}}. reason is None for below-threshold papers.
    Cached files are reused so interrupted runs can resume.

    Optional callbacks called as batches complete:
      haiku_callback(done: int, total: int)
      sonnet_callback(done: int, total: int)
    """
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    interests_path = PROJECT_ROOT / "config" / "interests.md"
    if not interests_path.exists():
        raise FileNotFoundError(
            f"{interests_path} not found. "
            f"Copy config/interests.md.example to config/interests.md and describe your research interests."
        )
    interests_text = interests_path.read_text()

    # Invalidate cache if the paper set has changed since the last run.
    manifest_path = STAGING_DIR / "manifest.json"
    run_fingerprint = hashlib.sha256(
        json.dumps(sorted(p["doi"] for p in papers)).encode()
    ).hexdigest()
    stored = {}
    if manifest_path.exists():
        try:
            stored = json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, Exception):
            pass
    if stored.get("fingerprint") != run_fingerprint:
        logger.info("Paper set changed — clearing staging cache")
        for f in STAGING_DIR.glob("scored_*.json"):
            f.unlink()
        for f in STAGING_DIR.glob("reasoned_*.json"):
            f.unlink()
        manifest_path.write_text(json.dumps({"fingerprint": run_fingerprint}))

    batches = []
    for i in range(0, len(papers), settings.batch_size):
        batches.append(papers[i : i + settings.batch_size])

    failures = 0

    # Write all batch inputs upfront so workers can read them immediately.
    batch_data_list = []
    for i, batch in enumerate(batches):
        batch_data = [
            {
                "doi": p["doi"],
                "title": p["title"],
                "abstract": p.get("abstract", "")[:1800],
            }
            for p in batch
        ]
        (STAGING_DIR / f"to_score_{i}.json").write_text(
            json.dumps(batch_data, indent=2)
        )
        batch_data_list.append(batch_data)

    # --- Stage 1: Haiku scoring (parallel, max 4 workers) ---
    def _haiku_batch(i: int) -> tuple[int, list | None]:
        input_path = STAGING_DIR / f"to_score_{i}.json"
        scored_path = STAGING_DIR / f"scored_{i}.json"
        if scored_path.exists():
            try:
                cached = json.loads(scored_path.read_text())
                logger.info(f"Batch {i}: loaded {len(cached)} cached Haiku scores")
                return i, cached
            except (json.JSONDecodeError, Exception):
                logger.warning(f"Batch {i}: cached scored file invalid, re-scoring")
                scored_path.unlink()
        prompt = _build_haiku_prompt(interests_text, batch_data_list[i])
        result = _run_claude(
            "claude-haiku-4-5-20251001",
            prompt,
            scored_path,
            i,
            "haiku",
        )
        if result is not None:
            logger.info(f"Batch {i}: Haiku scored {len(result)} papers")
        return i, result

    stage1_results: dict[int, list | None] = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_haiku_batch, i): i for i in range(len(batches))}
        for future in as_completed(futures):
            i, result = future.result()
            stage1_results[i] = result
            if haiku_callback:
                haiku_callback(len(stage1_results), len(batches))

    # --- Stage 2: Sonnet reasoning ---
    # Collect survivors across all Haiku batches in order, then rechunk at
    # sonnet_batch_size so each Sonnet call is fully utilised.
    all_survivors = []
    for i in range(len(batches)):
        s1 = stage1_results.get(i)
        if s1 is None:
            failures += 1
            continue
        paper_by_doi = {p["doi"]: p for p in batch_data_list[i]}
        for s in s1:
            doi = s.get("doi")
            score = s.get("score", 0)
            if doi and score >= settings.store_threshold and doi in paper_by_doi:
                all_survivors.append(
                    {
                        **paper_by_doi[doi],
                        "score": score,
                        "match_basis": s.get("match_basis"),
                    }
                )

    sonnet_batches = [
        all_survivors[i : i + settings.sonnet_batch_size]
        for i in range(0, len(all_survivors), settings.sonnet_batch_size)
    ]
    logger.info(
        f"{len(all_survivors)} survivors across {len(sonnet_batches)} Sonnet batch(es)"
    )
    if sonnet_start_callback and sonnet_batches:
        sonnet_start_callback(len(sonnet_batches))

    def _sonnet_batch(si: int) -> tuple[int, list | None]:
        chunk = sonnet_batches[si]
        input_path = STAGING_DIR / f"to_reason_s{si}.json"
        output_path = STAGING_DIR / f"reasoned_s{si}.json"
        if output_path.exists():
            try:
                cached = json.loads(output_path.read_text())
                logger.info(f"Sonnet batch {si}: loaded {len(cached)} cached reasons")
                return si, cached
            except (json.JSONDecodeError, Exception):
                logger.warning(f"Sonnet batch {si}: cached file invalid, re-reasoning")
                output_path.unlink()
        input_path.write_text(json.dumps(chunk, indent=2))
        prompt = _build_sonnet_prompt(interests_text, chunk)
        result = _run_claude(
            "claude-sonnet-4-6",
            prompt,
            output_path,
            si,
            "sonnet",
        )
        if result is not None:
            logger.info(f"Sonnet batch {si}: wrote reasons for {len(result)} papers")
        return si, result

    sonnet_results: dict[str, dict] = {}
    if sonnet_batches:
        completed_sonnet = 0
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(_sonnet_batch, si): si
                for si in range(len(sonnet_batches))
            }
            for future in as_completed(futures):
                si, result = future.result()
                completed_sonnet += 1
                if result:
                    for r in result:
                        if r.get("doi"):
                            sonnet_results[r["doi"]] = {
                                "score": r.get("score"),
                                "reason": r.get("reason"),
                            }
                else:
                    logger.warning(
                        f"Sonnet batch {si}: failed, scores/reasons unavailable for that chunk"
                    )
                if sonnet_callback:
                    sonnet_callback(completed_sonnet, len(sonnet_batches))

    # Yield original Haiku batches with merged scores + reasons.
    # Sonnet's score takes precedence over Haiku's for papers it re-evaluated.
    for i, batch in enumerate(batches):
        s1 = stage1_results.get(i)
        if s1 is None:
            continue
        haiku_scores = {
            s["doi"]: {"score": s["score"], "match_basis": s.get("match_basis")}
            for s in s1
            if s.get("doi")
        }
        batch_scores = {}
        for doi, haiku in haiku_scores.items():
            sonnet = sonnet_results.get(doi, {})
            batch_scores[doi] = {
                "score": sonnet.get("score") or haiku["score"],
                "reason": sonnet.get("reason"),
                "match_basis": haiku.get("match_basis"),
            }
        yield batch, batch_scores

    if failures == len(batches):
        logger.error("All scoring batches failed")
        return

    if failures == 0:
        for f in STAGING_DIR.glob("to_score_*.json"):
            f.unlink()
        for f in STAGING_DIR.glob("to_reason_*.json"):
            f.unlink()
        for f in STAGING_DIR.glob("scored_*.json"):
            f.unlink()
        for f in STAGING_DIR.glob("reasoned_*.json"):
            f.unlink()
        for f in STAGING_DIR.glob("reasoned_s*.json"):
            f.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        logger.info("Staging files cleared")
