"""Tests for the sieve export feature.

Covers:
- _parse_doi_file: plain text and BibTeX parsing
- build_bibliography: DB lookup, HTML rendering, missing DOIs, interests overlay
"""

import json
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from sieve.cli import _parse_doi_file
from sieve.generate import build_bibliography

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path, papers: list[dict]) -> Path:
    """Create a temp DB, patch db.DB_PATH, insert papers, return the path."""
    import sieve.db as db

    db_path = tmp_path / "papers.db"
    with patch.object(db, "DB_PATH", db_path):
        db.init_db()
        for p in papers:
            import sqlite3

            with sqlite3.connect(str(db_path)) as conn:
                conn.execute(
                    """INSERT INTO papers
                       (doi, title, authors, abstract, journal, published_date,
                        source, url, score, reason, match_basis, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                        "2024-01-01T00:00:00",
                    ),
                )
    return db_path


SAMPLE_PAPER = {
    "doi": "10.1038/s41593-023-01280-6",
    "title": "A connectomics study of cortical circuits",
    "authors": ["Alice Smith", "Bob Jones"],
    "abstract": "We mapped synaptic connectivity in mouse V1.",
    "journal": "Nature Neuroscience",
    "published_date": "2023-06-01",
    "source": "feed",
    "url": "https://doi.org/10.1038/s41593-023-01280-6",
    "score": 9,
    "reason": "Directly relevant to connectomics work.",
    "match_basis": "connectomics",
}


# ---------------------------------------------------------------------------
# _parse_doi_file: plain text
# ---------------------------------------------------------------------------


class TestParsePlainText:
    def test_basic(self, tmp_path):
        f = tmp_path / "dois.txt"
        f.write_text("10.1038/abc\n10.1101/def\n")
        assert _parse_doi_file(str(f), ignore_errors=False) == [
            "10.1038/abc",
            "10.1101/def",
        ]

    def test_comments_and_blanks_ignored(self, tmp_path):
        f = tmp_path / "dois.txt"
        f.write_text("# comment\n\n10.1038/abc\n\n# another\n10.1101/def\n")
        assert _parse_doi_file(str(f), ignore_errors=False) == [
            "10.1038/abc",
            "10.1101/def",
        ]

    def test_strips_url_prefix(self, tmp_path):
        f = tmp_path / "dois.txt"
        f.write_text("https://doi.org/10.1038/abc\nhttp://doi.org/10.1101/def\n")
        assert _parse_doi_file(str(f), ignore_errors=False) == [
            "10.1038/abc",
            "10.1101/def",
        ]


# ---------------------------------------------------------------------------
# _parse_doi_file: BibTeX
# ---------------------------------------------------------------------------


BIB_ALL_DOIS = textwrap.dedent("""\
    @article{smith2023,
      title = {Foo},
      doi = {10.1038/s41593-023-01280-6},
    }
    @article{jones2022,
      title = {Bar},
      doi = "10.1101/2022.01.01.000001",
    }
""")

BIB_MISSING_DOI = textwrap.dedent("""\
    @article{smith2023,
      title = {Foo},
      doi = {10.1038/s41593-023-01280-6},
    }
    @article{nodoi2020,
      title = {No DOI here},
    }
""")

BIB_URL_DOI = textwrap.dedent("""\
    @article{smith2023,
      doi = {https://doi.org/10.1038/s41593-023-01280-6},
    }
""")

BIB_WITH_SPECIAL_ENTRIES = textwrap.dedent("""\
    @string{pub = {Nature}}
    @comment{This is a comment}
    @article{real2023,
      doi = {10.1038/abc},
    }
""")


class TestParseBibTeX:
    def test_extracts_dois(self, tmp_path):
        f = tmp_path / "refs.bib"
        f.write_text(BIB_ALL_DOIS)
        result = _parse_doi_file(str(f), ignore_errors=False)
        assert result == [
            "10.1038/s41593-023-01280-6",
            "10.1101/2022.01.01.000001",
        ]

    def test_strips_url_prefix(self, tmp_path):
        f = tmp_path / "refs.bib"
        f.write_text(BIB_URL_DOI)
        result = _parse_doi_file(str(f), ignore_errors=False)
        assert result == ["10.1038/s41593-023-01280-6"]

    def test_special_entries_excluded_from_check(self, tmp_path):
        f = tmp_path / "refs.bib"
        f.write_text(BIB_WITH_SPECIAL_ENTRIES)
        result = _parse_doi_file(str(f), ignore_errors=False)
        assert result == ["10.1038/abc"]

    def test_missing_doi_ignore_errors(self, tmp_path):
        f = tmp_path / "refs.bib"
        f.write_text(BIB_MISSING_DOI)
        result = _parse_doi_file(str(f), ignore_errors=True)
        assert result == ["10.1038/s41593-023-01280-6"]

    def test_missing_doi_user_continues(self, tmp_path):
        f = tmp_path / "refs.bib"
        f.write_text(BIB_MISSING_DOI)
        with patch("builtins.input", return_value="y"):
            result = _parse_doi_file(str(f), ignore_errors=False)
        assert result == ["10.1038/s41593-023-01280-6"]

    def test_missing_doi_user_aborts(self, tmp_path):
        f = tmp_path / "refs.bib"
        f.write_text(BIB_MISSING_DOI)
        with patch("builtins.input", return_value="n"):
            with pytest.raises(SystemExit):
                _parse_doi_file(str(f), ignore_errors=False)

    def test_missing_doi_enter_aborts(self, tmp_path):
        f = tmp_path / "refs.bib"
        f.write_text(BIB_MISSING_DOI)
        with patch("builtins.input", return_value=""):
            with pytest.raises(SystemExit):
                _parse_doi_file(str(f), ignore_errors=False)


# ---------------------------------------------------------------------------
# build_bibliography
# ---------------------------------------------------------------------------


class TestBuildBibliography:
    def test_found_paper_in_html(self, tmp_path):
        db_path = _make_db(tmp_path, [SAMPLE_PAPER])
        out = tmp_path / "bib.html"
        import sieve.db as db

        with patch.object(db, "DB_PATH", db_path):
            found, missing = build_bibliography(
                [SAMPLE_PAPER["doi"]], out, title="Test Bib"
            )

        assert found == 1
        assert missing == []
        html = out.read_text()
        assert SAMPLE_PAPER["title"] in html
        assert SAMPLE_PAPER["doi"] in html
        assert "Test Bib" in html

    def test_missing_doi_returned(self, tmp_path):
        db_path = _make_db(tmp_path, [SAMPLE_PAPER])
        out = tmp_path / "bib.html"
        import sieve.db as db

        with patch.object(db, "DB_PATH", db_path):
            found, missing = build_bibliography(
                [SAMPLE_PAPER["doi"], "10.9999/nonexistent"], out
            )

        assert found == 1
        assert missing == ["10.9999/nonexistent"]

    def test_all_missing_renders_empty_page(self, tmp_path):
        db_path = _make_db(tmp_path, [])
        out = tmp_path / "bib.html"
        import sieve.db as db

        with patch.object(db, "DB_PATH", db_path):
            found, missing = build_bibliography(["10.9999/ghost"], out)

        assert found == 0
        assert missing == ["10.9999/ghost"]
        assert out.exists()

    def test_abstracts_open_by_default(self, tmp_path):
        db_path = _make_db(tmp_path, [SAMPLE_PAPER])
        out = tmp_path / "bib.html"
        import sieve.db as db

        with patch.object(db, "DB_PATH", db_path):
            build_bibliography([SAMPLE_PAPER["doi"]], out)

        html = out.read_text()
        # abstract div should carry the 'open' class
        assert 'class="abstract-body open' in html

    def test_no_server_endpoints_in_html(self, tmp_path):
        db_path = _make_db(tmp_path, [SAMPLE_PAPER])
        out = tmp_path / "bib.html"
        import sieve.db as db

        with patch.object(db, "DB_PATH", db_path):
            build_bibliography([SAMPLE_PAPER["doi"]], out)

        html = out.read_text()
        for endpoint in ["/mark-seen", "/toggle-seen", "/toggle-rl", "/summary"]:
            assert endpoint not in html

    def test_interests_overlay_applied(self, tmp_path):
        db_path = _make_db(tmp_path, [SAMPLE_PAPER])
        out = tmp_path / "bib.html"
        interests_file = tmp_path / "interests.md"
        interests_file.write_text("## Custom topic\n- EM segmentation\n")

        custom_annotations = {
            SAMPLE_PAPER["doi"]: {
                "score": 7,
                "reason": "Custom reason for EM segmentation context.",
            }
        }

        import sieve.db as db
        import sieve.score as score_mod

        with patch.object(db, "DB_PATH", db_path):
            with patch.object(
                score_mod, "annotate_papers", return_value=custom_annotations
            ):
                found, missing = build_bibliography(
                    [SAMPLE_PAPER["doi"]], out, interests_path=interests_file
                )

        html = out.read_text()
        assert "Custom reason for EM segmentation context." in html
        assert found == 1

    def test_interests_not_called_when_no_path(self, tmp_path):
        db_path = _make_db(tmp_path, [SAMPLE_PAPER])
        out = tmp_path / "bib.html"
        import sieve.db as db
        import sieve.score as score_mod

        with patch.object(db, "DB_PATH", db_path):
            with patch.object(score_mod, "annotate_papers") as mock_annotate:
                build_bibliography([SAMPLE_PAPER["doi"]], out)

        mock_annotate.assert_not_called()
