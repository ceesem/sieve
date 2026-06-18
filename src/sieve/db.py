import json
import random
import sqlite3
from datetime import datetime

from .settings import PROJECT_ROOT

DB_PATH = PROJECT_ROOT / "data" / "papers.db"

SCHEMA = """\
CREATE TABLE IF NOT EXISTS papers (
    doi             TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    authors         TEXT,
    abstract        TEXT,
    journal         TEXT,
    published_date  DATE,
    source          TEXT,
    url             TEXT,
    score           INTEGER,
    reason          TEXT,
    match_basis     TEXT,
    seen            INTEGER DEFAULT 0,
    reading_list    INTEGER DEFAULT 0,
    reading_list_at TEXT,
    rl_read         INTEGER DEFAULT 0,
    notes           TEXT,
    fetched_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_published_date ON papers(published_date);
CREATE INDEX IF NOT EXISTS idx_score ON papers(score);
CREATE INDEX IF NOT EXISTS idx_seen ON papers(seen);
CREATE INDEX IF NOT EXISTS idx_fetched_at ON papers(fetched_at);

-- Explicit "less like this" examples. A snapshot is stored so the signal
-- survives even after the source paper is pruned from `papers`.
CREATE TABLE IF NOT EXISTS negative_examples (
    doi             TEXT PRIMARY KEY,
    title           TEXT,
    abstract        TEXT,
    journal         TEXT,
    score           INTEGER,
    reason          TEXT,
    match_basis     TEXT,
    flagged_at      TEXT
);
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    for ddl in [
        "ALTER TABLE papers ADD COLUMN match_basis TEXT",
        "ALTER TABLE papers ADD COLUMN rl_read INTEGER DEFAULT 0",
        "ALTER TABLE papers ADD COLUMN reading_list_at TEXT",
    ]:
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # column already exists


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def get_existing_dois() -> set[str]:
    with _connect() as conn:
        rows = conn.execute("SELECT doi FROM papers").fetchall()
    return {r["doi"] for r in rows}


def insert_papers_with_scores(
    papers: list[dict], scores: dict[str, dict], fetched_at: str | None = None
) -> int:
    """Atomically insert papers that have matching scores."""
    now = fetched_at or datetime.now().isoformat()
    inserted = 0
    with _connect() as conn:
        for p in papers:
            doi = p["doi"]
            s = scores.get(doi)
            if s is None:
                continue
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO papers
                       (doi, title, authors, abstract, journal, published_date,
                        source, url, score, reason, match_basis, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        doi,
                        p["title"],
                        json.dumps(p.get("authors", [])),
                        p.get("abstract"),
                        p.get("journal"),
                        p.get("published_date"),
                        p.get("source"),
                        p.get("url"),
                        s.get("score"),
                        s.get("reason"),
                        s.get("match_basis"),
                        now,
                    ),
                )
                if conn.execute("SELECT changes()").fetchone()[0] > 0:
                    inserted += 1
            except sqlite3.Error:
                continue
    return inserted


def prune_papers(site_threshold: int, lookback_days: int) -> int:
    """Delete low-score papers outside the fetch window. Returns count deleted."""
    with _connect() as conn:
        conn.execute(
            """DELETE FROM papers
               WHERE score < ?
                 AND fetched_at < date('now', '-' || ? || ' days')
                 AND reading_list = 0""",
            (site_threshold, lookback_days),
        )
        return conn.execute("SELECT changes()").fetchone()[0]


def mark_all_seen() -> int:
    """Mark all papers as seen. Returns count updated."""
    with _connect() as conn:
        conn.execute("UPDATE papers SET seen = 1 WHERE seen = 0")
        return conn.execute("SELECT changes()").fetchone()[0]


def mark_seen(doi: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE papers SET seen = 1 WHERE doi = ?", (doi,))


def mark_unseen_bulk(dois: list[str]) -> int:
    """Mark a list of DOIs as unseen (seen=0). Returns count updated."""
    if not dois:
        return 0
    with _connect() as conn:
        placeholders = ",".join("?" * len(dois))
        cur = conn.execute(
            f"UPDATE papers SET seen = 0 WHERE doi IN ({placeholders})",
            dois,
        )
        return cur.rowcount


def toggle_seen(doi: str) -> int:
    """Toggle seen state. Returns new seen value (0 or 1)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE papers SET seen = CASE WHEN seen = 1 THEN 0 ELSE 1 END WHERE doi = ?",
            (doi,),
        )
        return conn.execute("SELECT seen FROM papers WHERE doi = ?", (doi,)).fetchone()[
            0
        ]


def toggle_rl_read(doi: str) -> int:
    """Toggle rl_read state. Returns new rl_read value (0 or 1)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE papers SET rl_read = CASE WHEN rl_read = 1 THEN 0 ELSE 1 END WHERE doi = ?",
            (doi,),
        )
        return conn.execute(
            "SELECT rl_read FROM papers WHERE doi = ?", (doi,)
        ).fetchone()[0]


def mark_all_rl_read() -> int:
    """Mark all reading list papers as rl_read. Returns count updated."""
    with _connect() as conn:
        conn.execute(
            "UPDATE papers SET rl_read = 1 WHERE reading_list = 1 AND rl_read = 0"
        )
        return conn.execute("SELECT changes()").fetchone()[0]


def toggle_reading_list(doi: str) -> None:
    # Stamp reading_list_at when adding (the CASE reads pre-update values); leave
    # it untouched when removing so a re-add keeps a fresh timestamp.
    now = datetime.now().isoformat()
    with _connect() as conn:
        conn.execute(
            """UPDATE papers
               SET reading_list = CASE WHEN reading_list = 1 THEN 0 ELSE 1 END,
                   reading_list_at = CASE WHEN reading_list = 1
                                          THEN reading_list_at ELSE ? END
               WHERE doi = ?""",
            (now, doi),
        )


def toggle_negative_example(doi: str) -> int:
    """Toggle a paper's membership in the negative-examples table.

    Adds a self-contained snapshot (so the signal outlives pruning) or removes
    it. Returns 1 if the paper is now flagged, 0 if it was un-flagged.
    """
    now = datetime.now().isoformat()
    with _connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM negative_examples WHERE doi = ?", (doi,)
        ).fetchone()
        if exists:
            conn.execute("DELETE FROM negative_examples WHERE doi = ?", (doi,))
            return 0
        conn.execute(
            """INSERT INTO negative_examples
                   (doi, title, abstract, journal, score, reason, match_basis, flagged_at)
               SELECT doi, title, abstract, journal, score, reason, match_basis, ?
               FROM papers WHERE doi = ?""",
            (now, doi),
        )
        return 1


def get_negative_examples(limit: int | None = 40) -> list[dict]:
    """Return explicit 'less like this' examples, newest first (capped)."""
    sql = "SELECT * FROM negative_examples ORDER BY flagged_at DESC"
    params: tuple = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_positive_examples(
    recent_k: int | None = 50, older_sample: int = 25
) -> list[dict]:
    """Return reading-list papers — the natural 'more like this' signal.

    Graded recency sample: the `recent_k` most recently saved papers in full,
    plus a random draw of up to `older_sample` from everything older. This keeps
    current interests fully represented without over-fitting to the latest
    publications, while still surfacing long-standing themes — and because the
    older draw is random, repeated runs sweep different slices of the tail.

    recent_k=None returns every reading-list paper (full rebuild).
    """
    with _connect() as conn:
        rows = conn.execute(
            """SELECT doi, title, abstract, journal, score, reason, match_basis
               FROM papers WHERE reading_list = 1
               ORDER BY reading_list_at DESC, score DESC"""
        ).fetchall()
    papers = [dict(r) for r in rows]
    if recent_k is None:
        return papers
    recent, older = papers[:recent_k], papers[recent_k:]
    if older and older_sample > 0:
        recent += random.sample(older, min(older_sample, len(older)))
    return recent


def set_note(doi: str, note: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE papers SET notes = ? WHERE doi = ?", (note, doi))


def get_paper(doi: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM papers WHERE doi = ?", (doi,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["authors"] = json.loads(d["authors"]) if d["authors"] else []
    return d


def get_papers_for_display(days: int = 30, site_threshold: int = 4) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """SELECT p.*,
                      EXISTS(SELECT 1 FROM negative_examples n WHERE n.doi = p.doi)
                          AS is_negative
               FROM papers p
               WHERE (p.fetched_at >= date('now', ?) OR p.reading_list = 1)
                 AND (p.score >= ? OR p.reading_list = 1)
               ORDER BY p.score DESC, p.published_date DESC""",
            (f"-{days} days", site_threshold),
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["authors"] = json.loads(d["authors"]) if d["authors"] else []
        result.append(d)
    return result


def get_summary(
    display_threshold: int = 7, days: int = 30, site_threshold: int = 4
) -> dict:
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        unread = conn.execute(
            """SELECT COUNT(*) FROM papers
               WHERE seen = 0
                 AND (fetched_at >= date('now', ?) OR reading_list = 1)
                 AND (score >= ? OR reading_list = 1)""",
            (f"-{days} days", site_threshold),
        ).fetchone()[0]
        rl_unread = conn.execute(
            "SELECT COUNT(*) FROM papers WHERE reading_list = 1 AND rl_read = 0"
        ).fetchone()[0]
        high_score = conn.execute(
            "SELECT COUNT(*) FROM papers WHERE score >= ? AND seen = 0",
            (display_threshold,),
        ).fetchone()[0]
        row = conn.execute("SELECT MAX(fetched_at) FROM papers").fetchone()
        last_fetched = row[0] if row else None
    return {
        "total": total,
        "unread": unread,
        "rl_unread": rl_unread,
        "high_score": high_score,
        "last_fetched": last_fetched,
    }
