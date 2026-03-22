import logging

from . import db

logger = logging.getLogger(__name__)


def ingest_batch(
    papers: list[dict], scores: dict[str, dict], fetched_at: str | None = None
) -> int:
    """Insert scored papers atomically. Returns count inserted."""
    inserted = db.insert_papers_with_scores(papers, scores, fetched_at)
    logger.info(f"Ingested {inserted} papers")
    return inserted
