"""Tests for the interests-feedback signals backing `sieve learn`.

Covers the database layer that records explicit "less like this" examples and
exposes reading-list papers as positives:
- toggle_negative_example: add / remove, snapshot capture
- get_positive_examples / get_negative_examples
- get_papers_for_display exposes is_negative
- negative-example snapshots survive pruning of the source paper
"""

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import sieve.db as db


def _make_db(tmp_path: Path, papers: list[dict]) -> Path:
    db_path = tmp_path / "papers.db"
    with patch.object(db, "DB_PATH", db_path):
        db.init_db()
        for p in papers:
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute(
                    """INSERT INTO papers
                       (doi, title, authors, abstract, journal, published_date,
                        source, url, score, reason, match_basis, reading_list,
                        fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        p["doi"],
                        p["title"],
                        json.dumps(p.get("authors", [])),
                        p.get("abstract"),
                        p.get("journal"),
                        p.get("published_date"),
                        p.get("source", "feed"),
                        p.get("url"),
                        p.get("score"),
                        p.get("reason"),
                        p.get("match_basis"),
                        p.get("reading_list", 0),
                        p.get("fetched_at", "2026-06-18T00:00:00"),
                    ),
                )
    return db_path


PAPERS = [
    {
        "doi": "10.1/pos",
        "title": "Cortical interneuron connectivity",
        "score": 9,
        "reason": "directly your area",
        "match_basis": "interneuron cortical circuits",
        "reading_list": 1,
    },
    {
        "doi": "10.1/neg",
        "title": "Unrelated ML benchmark",
        "score": 7,
        "reason": "weak match",
        "match_basis": "ML methods",
    },
]


def test_toggle_negative_adds_and_removes(tmp_path):
    db_path = _make_db(tmp_path, PAPERS)
    with patch.object(db, "DB_PATH", db_path):
        assert db.toggle_negative_example("10.1/neg") == 1
        assert [p["doi"] for p in db.get_negative_examples()] == ["10.1/neg"]
        assert db.toggle_negative_example("10.1/neg") == 0
        assert db.get_negative_examples() == []


def test_negative_example_snapshots_fields(tmp_path):
    db_path = _make_db(tmp_path, PAPERS)
    with patch.object(db, "DB_PATH", db_path):
        db.toggle_negative_example("10.1/neg")
        (ex,) = db.get_negative_examples()
    assert ex["title"] == "Unrelated ML benchmark"
    assert ex["score"] == 7
    assert ex["match_basis"] == "ML methods"
    assert ex["flagged_at"]


def test_reading_list_is_positive_signal(tmp_path):
    db_path = _make_db(tmp_path, PAPERS)
    with patch.object(db, "DB_PATH", db_path):
        pos = db.get_positive_examples()
    assert [p["doi"] for p in pos] == ["10.1/pos"]
    assert pos[0]["reason"] == "directly your area"


def test_display_exposes_is_negative(tmp_path):
    db_path = _make_db(tmp_path, PAPERS)
    with patch.object(db, "DB_PATH", db_path):
        db.toggle_negative_example("10.1/neg")
        disp = db.get_papers_for_display(days=3650, site_threshold=0)
    flags = {p["doi"]: p["is_negative"] for p in disp}
    assert flags["10.1/neg"] == 1
    assert flags["10.1/pos"] == 0


def test_toggle_reading_list_stamps_time(tmp_path):
    db_path = _make_db(tmp_path, [PAPERS[1]])  # 10.1/neg, not on reading list
    with patch.object(db, "DB_PATH", db_path):
        db.toggle_reading_list("10.1/neg")
        p = db.get_paper("10.1/neg")
        assert p["reading_list"] == 1
        assert p["reading_list_at"]  # stamped on add
        db.toggle_reading_list("10.1/neg")
        assert db.get_paper("10.1/neg")["reading_list"] == 0


def test_graded_recency_sample(tmp_path):
    papers = [
        {"doi": f"10.1/{i}", "title": f"t{i}", "abstract": f"abstract {i}", "score": 5}
        for i in range(5)
    ]
    db_path = _make_db(tmp_path, papers)
    with patch.object(db, "DB_PATH", db_path):
        # doi 0 oldest … doi 4 newest
        with sqlite3.connect(str(db_path)) as conn:
            for i in range(5):
                conn.execute(
                    "UPDATE papers SET reading_list=1, reading_list_at=? WHERE doi=?",
                    (f"2026-01-0{i + 1}T00:00:00", f"10.1/{i}"),
                )
        sample = db.get_positive_examples(recent_k=2, older_sample=1)
        dois = [p["doi"] for p in sample]
        assert dois[:2] == ["10.1/4", "10.1/3"]  # recent always included, newest first
        assert len(sample) == 3  # + one random older
        assert dois[2] in {"10.1/0", "10.1/1", "10.1/2"}
        assert all(p.get("abstract") for p in sample)  # abstracts carried through
        # full rebuild returns everything
        assert len(db.get_positive_examples(recent_k=None)) == 5


def test_negative_examples_capped(tmp_path):
    papers = [{"doi": f"10.1/n{i}", "title": f"t{i}", "score": 5} for i in range(5)]
    db_path = _make_db(tmp_path, papers)
    with patch.object(db, "DB_PATH", db_path):
        for i in range(5):
            db.toggle_negative_example(f"10.1/n{i}")
        assert len(db.get_negative_examples(limit=3)) == 3
        assert len(db.get_negative_examples(limit=None)) == 5


def test_negative_snapshot_survives_prune(tmp_path):
    """The point of a separate table: signal outlives the pruned paper."""
    db_path = _make_db(tmp_path, PAPERS)
    with patch.object(db, "DB_PATH", db_path):
        db.toggle_negative_example("10.1/neg")
        # Make the source paper prunable: low score, old, not on reading list.
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                "UPDATE papers SET score=1, fetched_at='2020-01-01' WHERE doi='10.1/neg'"
            )
        deleted = db.prune_papers(site_threshold=4, lookback_days=2)
        assert deleted == 1
        # Source paper is gone, but the negative example remains.
        assert db.get_paper("10.1/neg") is None
        assert [p["doi"] for p in db.get_negative_examples()] == ["10.1/neg"]
