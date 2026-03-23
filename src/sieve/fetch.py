import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import feedparser
import httpx
from tenacity import before_sleep_log, retry, stop_after_attempt, wait_exponential

from .db import get_existing_dois
from .normalize import normalize_paper
from .settings import Settings

logger = logging.getLogger(__name__)

_retry = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=4, max=30),
    reraise=True,
    before_sleep=before_sleep_log(logger, logging.WARNING),
)

DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s\"<>]+)")

_AFFILIATION_KEYWORDS = re.compile(
    r"(?:University|Institute|Department|School|Center|Laboratory|"
    r"College|Hospital|National|Research|Sciences?|Technology|Foundation)"
)


def _split_concatenated_authors(raw: str) -> list[str]:
    """Handle feeds (e.g. PNAS) that concatenate authors+affiliations into one string.

    Splits at CamelCase boundaries, stops when address-like affiliation text is
    detected, and strips trailing superscript affiliation markers (a, b, c...).
    """
    parts = re.split(r"(?<=[a-z])(?=[A-Z][a-z])", raw)
    names = []
    for p in parts:
        # Stop when we hit affiliation text: long string with commas or institution keywords
        if ("," in p and len(p) > 30) or (
            _AFFILIATION_KEYWORDS.search(p) and len(p) > 20
        ):
            # Strip the affiliation superscript only from the last name before the block
            if names:
                names[-1] = re.sub(r"\s*[a-e]$", "", names[-1]).strip()
            break
        if p.strip():
            names.append(p.strip())
    return [n for n in names if n]


def fetch_biorxiv(settings: Settings) -> list[dict]:
    end = date.today()
    start = end - timedelta(days=settings.lookback_days)
    papers = []
    cursor = 0
    client = httpx.Client(timeout=30)

    while len(papers) < settings.max_papers_per_source:
        url = f"https://api.biorxiv.org/details/biorxiv/{start}/{end}/{cursor}/json"
        try:
            resp = _retry(client.get)(url)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"bioRxiv fetch error at cursor {cursor}: {e}")
            break

        collection = data.get("collection", [])
        if not collection:
            break

        for item in collection:
            if item.get("category", "").lower() != settings.biorxiv_category:
                continue
            doi = item.get("doi")
            abstract = item.get("abstract")
            if not doi or not abstract:
                continue
            authors = [
                a.strip() for a in item.get("authors", "").split(";") if a.strip()
            ]
            papers.append(
                {
                    "doi": doi,
                    "title": item.get("title", ""),
                    "authors": authors,
                    "abstract": abstract,
                    "journal": "bioRxiv",
                    "published_date": item.get("date"),
                    "source": "biorxiv",
                    "url": f"https://doi.org/{doi}",
                }
            )

        messages = data.get("messages", [{}])
        status = messages[0].get("status", "") if messages else ""
        if "no entries" in status.lower() or len(collection) < 30:
            break
        cursor += len(collection)

    client.close()
    logger.info(f"bioRxiv: fetched {len(papers)} papers")
    return papers[: settings.max_papers_per_source]


def _fetch_arxiv_category(cat: str) -> list[dict]:
    """Fetch and parse a single arXiv RSS category. Returns list of papers."""
    url = f"https://rss.arxiv.org/rss/{cat}"
    try:
        resp = _retry(httpx.get)(url, timeout=30)
        feed = feedparser.parse(resp.text)
    except Exception as e:
        logger.error(f"arXiv RSS error for {cat}: {e}")
        return []

    papers = []
    for entry in feed.entries:
        arxiv_id = entry.get("id", entry.get("link", ""))
        id_match = re.search(r"(\d{4}\.\d{4,5})", arxiv_id)
        if id_match:
            arxiv_id = id_match.group(1)

        doi = f"arxiv:{arxiv_id}"
        title = entry.get("title", "")
        abstract = entry.get("summary", "")
        if not abstract:
            continue

        authors = []
        for a in entry.get("authors", []):
            name = a.get("name", "")
            if name:
                authors.append(name)
        if not authors and entry.get("author"):
            authors = [entry["author"]]

        papers.append(
            {
                "doi": doi,
                "title": title.strip(),
                "authors": authors,
                "abstract": abstract.strip(),
                "journal": "arXiv",
                "published_date": date.today().isoformat(),
                "source": "arxiv",
                "url": f"https://arxiv.org/abs/{arxiv_id}",
            }
        )
    return papers


def fetch_arxiv(settings: Settings) -> list[dict]:
    papers: list[dict] = []
    with ThreadPoolExecutor(
        max_workers=len(settings.arxiv_categories) or 1
    ) as executor:
        futures = {
            executor.submit(_fetch_arxiv_category, cat): cat
            for cat in settings.arxiv_categories
        }
        for future in as_completed(futures):
            papers.extend(future.result())
    logger.info(f"arXiv: fetched {len(papers)} papers")
    return papers[: settings.max_papers_per_source]


def _fetch_single_feed(feed_conf, client: httpx.Client) -> list[dict]:
    """Fetch and parse a single RSS feed. Returns list of papers."""
    try:
        resp = _retry(client.get)(feed_conf.url)
        feed = feedparser.parse(resp.text)
    except Exception as e:
        logger.error(f"Feed error for {feed_conf.name}: {e}")
        return []

    feed_papers = []
    no_doi = 0
    for entry in feed.entries:
        doi = None
        for field in [
            entry.get("prism_doi", ""),
            entry.get("dc_identifier", ""),
            entry.get("id", ""),
            entry.get("link", ""),
            entry.get("doi", ""),
        ]:
            m = DOI_RE.search(str(field))
            if m:
                doi = m.group(1).rstrip(".")
                break
        if not doi:
            no_doi += 1
            continue

        abstract = entry.get("summary", "")

        authors = []
        for a in entry.get("authors", []):
            name = a.get("name", "")
            if name:
                authors.append(name)
        if not authors and entry.get("author"):
            authors = [entry["author"]]
        # Some feeds (e.g. PNAS) concatenate all authors+affiliations into one string
        if len(authors) == 1 and _AFFILIATION_KEYWORDS.search(authors[0]):
            authors = _split_concatenated_authors(authors[0])

        feed_papers.append(
            {
                "doi": doi,
                "title": entry.get("title", "").strip(),
                "authors": authors,
                "abstract": abstract.strip() if abstract else "",
                "journal": feed_conf.name,
                "published_date": date.today().isoformat(),
                "source": "feed",
                "url": entry.get("link", f"https://doi.org/{doi}"),
            }
        )

    has_abstract = sum(1 for p in feed_papers if p.get("abstract"))
    logger.info(
        f"  {feed_conf.name}: {len(feed.entries)} entries → "
        f"{len(feed_papers)} with DOI ({no_doi} no DOI), "
        f"{has_abstract} with abstract"
    )
    return feed_papers


def fetch_feeds(settings: Settings) -> list[dict]:
    papers: list[dict] = []
    client = httpx.Client(timeout=30, follow_redirects=True)
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(_fetch_single_feed, fc, client): fc for fc in settings.feeds
        }
        for future in as_completed(futures):
            papers.extend(future.result())
    client.close()
    logger.info(f"Feeds: fetched {len(papers)} papers total")
    return papers


def _fetch_abstract_for_paper(p: dict, client: httpx.Client) -> tuple[dict, str]:
    """Try CrossRef then Semantic Scholar for a single paper missing an abstract.

    Returns (paper, source) where source is 'crossref', 's2', or 'none'.
    """
    doi = p["doi"]

    # 1. Try CrossRef
    try:
        resp = client.get(f"https://api.crossref.org/works/{doi}", timeout=15)
        if resp.status_code == 200:
            abstract = resp.json().get("message", {}).get("abstract", "")
            if abstract:
                # Strip JATS XML tags
                abstract = re.sub(r"<[^>]+>", "", abstract).strip()
                if abstract:
                    p["abstract"] = abstract
                    return p, "crossref"
    except Exception:
        pass

    # 2. Fall back to Semantic Scholar
    try:
        resp = client.get(
            f"https://api.semanticscholar.org/graph/v1/paper/{doi}",
            params={"fields": "abstract"},
            timeout=15,
        )
        if resp.status_code == 200:
            abstract = resp.json().get("abstract", "")
            if abstract:
                p["abstract"] = abstract
                return p, "s2"
    except Exception:
        pass

    return p, "none"


def _enrich_abstracts(papers: list[dict]) -> list[dict]:
    """Best-effort abstract enrichment via CrossRef then Semantic Scholar.

    Papers without abstracts are kept (title-only) rather than dropped.
    """
    need_enrichment = [p for p in papers if not p.get("abstract")]
    already_have = [p for p in papers if p.get("abstract")]

    if not need_enrichment:
        return papers

    filled_crossref = 0
    filled_s2 = 0
    kept_empty = 0

    client = httpx.Client(timeout=15, follow_redirects=True)
    enriched_papers = list(already_have)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(_fetch_abstract_for_paper, p, client): p
            for p in need_enrichment
        }
        for future in as_completed(futures):
            paper, source = future.result()
            enriched_papers.append(paper)
            if source == "crossref":
                filled_crossref += 1
            elif source == "s2":
                filled_s2 += 1
            else:
                kept_empty += 1
                logger.debug(
                    f"  No abstract for {paper['journal']}: {paper['title'][:60]}"
                )

    client.close()
    logger.info(
        f"Abstract enrichment: {filled_crossref} via CrossRef, "
        f"{filled_s2} via Semantic Scholar, "
        f"{kept_empty} kept with no abstract (title-only)"
    )
    return enriched_papers


def fetch_all(settings: Settings) -> list[dict]:
    existing_dois = get_existing_dois()

    # Run all three sources in parallel; enrich feed abstracts after feeds complete.
    with ThreadPoolExecutor(max_workers=3) as executor:
        f_biorxiv = executor.submit(fetch_biorxiv, settings)
        f_arxiv = executor.submit(fetch_arxiv, settings)
        f_feeds = executor.submit(fetch_feeds, settings)
        biorxiv = f_biorxiv.result()
        arxiv = f_arxiv.result()
        feeds = f_feeds.result()

    feeds = _enrich_abstracts(feeds)

    # Dedup: prefer biorxiv > arxiv > feed
    seen_dois: dict[str, dict] = {}
    source_priority = {"biorxiv": 0, "arxiv": 1, "feed": 2}

    for paper in [normalize_paper(p) for p in biorxiv + arxiv + feeds]:
        doi = paper["doi"]
        if doi in existing_dois:
            continue
        if doi in seen_dois:
            existing_priority = source_priority.get(seen_dois[doi]["source"], 99)
            new_priority = source_priority.get(paper["source"], 99)
            if new_priority < existing_priority:
                seen_dois[doi] = paper
        else:
            seen_dois[doi] = paper

    result = list(seen_dois.values())
    logger.info(f"Total after dedup: {len(result)} new papers")
    return result
