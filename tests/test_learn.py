"""Tests for the `sieve learn` interests-tuning helpers.

Covers the pure, non-interactive pieces:
- _build_learn_prompt advertises add / revise / remove with recall-biased guidance
- _format_examples carries full abstracts
- _backup_interests writes a timestamped copy before destructive edits
"""

from pathlib import Path

import sieve.seed as seed


def test_prompt_offers_all_three_edit_types():
    prompt = seed._build_learn_prompt("## Interests\n- connectomics\n", [], [])
    for token in ('"additions"', '"revisions"', '"removals"'):
        assert token in prompt
    # recall bias + safety guidance
    assert "RECALL over precision" in prompt
    assert "REMOVE conservatively" in prompt
    assert "VERBATIM" in prompt


def test_format_examples_includes_full_abstract():
    long_abstract = "We map cortical microcircuits. " * 40  # > any old 400-char cap
    block = seed._format_examples(
        [{"title": "T", "reason": "matches", "abstract": long_abstract}]
    )
    assert "abstract:" in block
    assert long_abstract.strip() in block  # not truncated


def test_backup_interests_writes_timestamped_copy(tmp_path, monkeypatch):
    src = tmp_path / "interests.md"
    src.write_text("## Interests\n- a\n- b\n")
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(seed, "INTERESTS_BACKUP_DIR", backup_dir)

    dest = seed._backup_interests(src)

    assert Path(dest).exists()
    assert Path(dest).read_text() == src.read_text()
    assert Path(dest).name.startswith("interests-")
    assert Path(dest).suffix == ".md"
