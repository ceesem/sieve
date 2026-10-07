import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import feedparser
import httpx
from tenacity import before_sleep_log, retry, stop_after_attempt, wait_exponential

from .db import get_existing_dois
from .normalize import normalize_paper
from .settings import PROJECT_ROOT, Settings

logger = logging.getLogger(__name__)

_retry = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=4, max=30),
    reraise=True,
    before_sleep=before_sleep_log(logger, logging.WARNING),
)

# Publisher CDNs (nature.com, academic.oup.com) block the default httpx
# User-Agent; a browser-like UA gets real RSS back.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    ),
}

# Papers fetched but never scored (failed batch, logged-out CLI, DOI dropped
# from the model's output). Retried on later runs, since arXiv RSS and journal
# "current issue" feeds will not serve them again.
PENDING_PATH = PROJECT_ROOT / "data" / "pending.json"
MAX_PENDING_ATTEMPTS = 5

# Feed "abstracts" shorter than this are usually teasers (nature.com, PNAS),
# so try to replace them with the full abstract from CrossRef / S2.
SHORT_ABSTRACT_LEN = 400

# nature.com article pages carry the full abstract in <meta name="dc.description">.
# CrossRef has no Nature abstracts and S2 lags new papers by days, so this is
# the only same-day source for Nature-family research articles (10.1038/s4…).
_NATURE_DESC_RE = re.compile(r'<meta\s+name="dc\.description"\s+content="([^"]*)"')

DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s\"<>]+)")

_AFFILIATION_KEYWORDS = re.compile(
    r"(?:University|Institute|Department|School|Center|Laboratory|"
    r"College|Hospital|National|Research|Sciences?|Technology|Foundation)"
)


def _report(report: dict | None, source: str, entries: int, error: str | None = None):
    """Record one source's fetch outcome for the health check."""
    if report is not None:
        if error:  # httpx errors span several lines; keep the first
            error = error.strip().splitlines()[0][:200]
        report[source] = {"entries": entries, "error": error}


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


def fetch_biorxiv(settings: Settings, report: dict | None = None) -> list[dict]:
    end = date.today()
    start = end - timedelta(days=settings.lookback_days)
    papers = []
    cursor = 0
    client = httpx.Client(timeout=30)
    error = None

    while len(papers) < settings.max_papers_per_source:
        url = f"https://api.biorxiv.org/details/biorxiv/{start}/{end}/{cursor}/json"
        try:
            resp = _retry(client.get)(url)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"bioRxiv fetch error at cursor {cursor}: {e}")
            if cursor == 0:
                error = str(e)
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
                    "feed": "bioRxiv",
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
    _report(report, "bioRxiv", len(papers), error)
    return papers[: settings.max_papers_per_source]


def _fetch_arxiv_category(cat: str, report: dict | None = None) -> list[dict]:
    """Fetch and parse a single arXiv RSS category. Returns list of papers."""
    url = f"https://rss.arxiv.org/rss/{cat}"
    try:
        resp = _retry(httpx.get)(url, timeout=30, headers=BROWSER_HEADERS)
        resp.raise_for_status()
        feed = feedparser.parse(resp.text)
    except Exception as e:
        logger.error(f"arXiv RSS error for {cat}: {e}")
        _report(report, f"arXiv {cat}", 0, str(e))
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
                "feed": f"arXiv {cat}",
                "url": f"https://arxiv.org/abs/{arxiv_id}",
            }
        )
    _report(report, f"arXiv {cat}", len(papers))
    return papers


def fetch_arxiv(settings: Settings, report: dict | None = None) -> list[dict]:
    papers: list[dict] = []
    with ThreadPoolExecutor(
        max_workers=len(settings.arxiv_categories) or 1
    ) as executor:
        futures = {
            executor.submit(_fetch_arxiv_category, cat, report): cat
            for cat in settings.arxiv_categories
        }
        for future in as_completed(futures):
            papers.extend(future.result())
    logger.info(f"arXiv: fetched {len(papers)} papers")
    return papers[: settings.max_papers_per_source]


def _fetch_single_feed(
    feed_conf, client: httpx.Client, settings: Settings, report: dict | None = None
) -> list[dict]:
    """Fetch and parse a single RSS feed. Returns list of papers."""
    if feed_conf.issn:
        return _fetch_crossref_journal(feed_conf, settings, client, report)
    try:
        resp = _retry(client.get)(feed_conf.url)
        resp.raise_for_status()
        feed = feedparser.parse(resp.text)
    except Exception as e:
        logger.error(f"Feed error for {feed_conf.name}: {e}")
        _report(report, feed_conf.name, 0, str(e))
        return []
    # feedparser never raises: a bot-challenge HTML page parses as an empty
    # feed. Surface it instead of silently reporting "0 entries".
    if not feed.entries and "html" in resp.headers.get("content-type", ""):
        logger.error(
            f"Feed error for {feed_conf.name}: got HTML instead of RSS "
            f"(likely bot protection) from {feed_conf.url}"
        )
        _report(report, feed_conf.name, 0, "got HTML instead of RSS (bot protection?)")
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
                "feed": feed_conf.name,
                "url": entry.get("link", f"https://doi.org/{doi}"),
            }
        )

    has_abstract = sum(1 for p in feed_papers if p.get("abstract"))
    logger.info(
        f"  {feed_conf.name}: {len(feed.entries)} entries → "
        f"{len(feed_papers)} with DOI ({no_doi} no DOI), "
        f"{has_abstract} with abstract"
    )
    _report(report, feed_conf.name, len(feed_papers))
    return feed_papers


def _fetch_crossref_journal(
    feed_conf, settings: Settings, client: httpx.Client, report: dict | None = None
) -> list[dict]:
    """Fetch recent articles for a journal by ISSN from the CrossRef API.

    Used for publishers whose RSS sits behind a JS bot challenge (Cell Press).
    CrossRef rarely carries abstracts for these, so most papers arrive
    title-only and go through the normal abstract enrichment.
    """
    since = date.today() - timedelta(days=max(settings.lookback_days, 7))
    params = {
        "filter": f"from-pub-date:{since},type:journal-article",
        "rows": 200,
        "sort": "published",
        "order": "desc",
        "select": "DOI,title,author,abstract,URL",
    }
    if settings.mailto:
        params["mailto"] = settings.mailto
    try:
        resp = _retry(client.get)(
            f"https://api.crossref.org/journals/{feed_conf.issn}/works", params=params
        )
        resp.raise_for_status()
        items = resp.json().get("message", {}).get("items", [])
    except Exception as e:
        logger.error(f"CrossRef error for {feed_conf.name}: {e}")
        _report(report, feed_conf.name, 0, str(e))
        return []

    papers = []
    for item in items:
        doi = item.get("DOI")
        title = (item.get("title") or [""])[0]
        if not doi or not title:
            continue
        authors = [
            f"{a.get('given', '')} {a.get('family', '')}".strip()
            for a in item.get("author", [])
            if a.get("family")
        ]
        papers.append(
            {
                "doi": doi.lower(),
                "title": title.strip(),
                "authors": authors,
                "abstract": item.get("abstract", ""),
                "journal": feed_conf.name,
                "published_date": date.today().isoformat(),
                "source": "feed",
                "feed": feed_conf.name,
                "url": item.get("URL") or f"https://doi.org/{doi}",
            }
        )
    logger.info(f"  {feed_conf.name}: {len(papers)} recent articles via CrossRef")
    _report(report, feed_conf.name, len(papers))
    return papers


def fetch_feeds(settings: Settings, report: dict | None = None) -> list[dict]:
    papers: list[dict] = []
    client = httpx.Client(timeout=30, follow_redirects=True, headers=BROWSER_HEADERS)
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(_fetch_single_feed, fc, client, settings, report): fc
            for fc in settings.feeds
        }
        for future in as_completed(futures):
            papers.extend(future.result())
    client.close()
    logger.info(f"Feeds: fetched {len(papers)} papers total")
    return papers


def _fetch_abstract_for_paper(p: dict, client: httpx.Client) -> tuple[dict, str]:
    """Try CrossRef then Semantic Scholar for a paper with a missing or teaser abstract.

    Only replaces the existing text when the fetched abstract is longer.

    Returns (paper, source) where source is 'crossref', 's2', or 'none'.
    """
    doi = p["doi"]
    current = len(p.get("abstract") or "")

    # 0. nature.com research article page
    if doi.startswith("10.1038/s4"):
        try:
            resp = client.get(f"https://www.nature.com/articles/{doi.split('/', 1)[1]}")
            m = _NATURE_DESC_RE.search(resp.text) if resp.status_code == 200 else None
            if m and len(m.group(1)) > current:
                p["abstract"] = m.group(1)
                return p, "nature"
        except Exception:
            pass

    # 1. Try CrossRef
    try:
        resp = client.get(f"https://api.crossref.org/works/{doi}", timeout=15)
        if resp.status_code == 200:
            abstract = resp.json().get("message", {}).get("abstract", "")
            if abstract:
                # Strip JATS XML tags
                abstract = re.sub(r"<[^>]+>", "", abstract).strip()
                if len(abstract) > current:
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
            abstract = resp.json().get("abstract") or ""
            if len(abstract) > current:
                p["abstract"] = abstract
                return p, "s2"
    except Exception:
        pass

    return p, "none"


def _enrich_abstracts(papers: list[dict]) -> list[dict]:
    """Best-effort abstract enrichment via CrossRef then Semantic Scholar.

    Papers without abstracts are kept (title-only) rather than dropped, and
    short teasers are kept when no full abstract can be found.
    """
    need_enrichment = [
        p for p in papers if len(p.get("abstract") or "") < SHORT_ABSTRACT_LEN
    ]
    already_have = [
        p for p in papers if len(p.get("abstract") or "") >= SHORT_ABSTRACT_LEN
    ]

    if not need_enrichment:
        return papers

    filled_nature = 0
    filled_crossref = 0
    filled_s2 = 0
    kept_empty = 0

    client = httpx.Client(timeout=15, follow_redirects=True, headers=BROWSER_HEADERS)
    enriched_papers = list(already_have)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(_fetch_abstract_for_paper, p, client): p
            for p in need_enrichment
        }
        for future in as_completed(futures):
            paper, source = future.result()
            enriched_papers.append(paper)
            if source == "nature":
                filled_nature += 1
            elif source == "crossref":
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
        f"Abstract enrichment: {filled_nature} via nature.com, "
        f"{filled_crossref} via CrossRef, "
        f"{filled_s2} via Semantic Scholar, "
        f"{kept_empty} kept as-is (teaser or title-only)"
    )
    return enriched_papers


def load_pending() -> list[dict]:
    try:
        return json.loads(PENDING_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_pending(papers: list[dict]) -> None:
    """Persist unscored papers for retry, dropping any that keep failing."""
    keep = []
    for p in papers:
        p = {**p, "pending_attempts": p.get("pending_attempts", 0) + 1}
        if p["pending_attempts"] < MAX_PENDING_ATTEMPTS:
            keep.append(p)
        else:
            logger.warning(f"Giving up on unscored paper {p['doi']}: {p['title'][:60]}")
    if keep:
        PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
        PENDING_PATH.write_text(json.dumps(keep))
        logger.warning(f"{len(keep)} unscored papers queued for the next run")
    else:
        PENDING_PATH.unlink(missing_ok=True)


def fetch_all(settings: Settings, report: dict | None = None) -> list[dict]:
    """Fetch new papers from every source.

    If `report` is given, it is filled with per-source health stats:
    {source: {entries, error, new, full_abstract}}.
    """
    existing_dois = get_existing_dois()
    report = {} if report is None else report

    # Run all three sources in parallel.
    with ThreadPoolExecutor(max_workers=3) as executor:
        f_biorxiv = executor.submit(fetch_biorxiv, settings, report)
        f_arxiv = executor.submit(fetch_arxiv, settings, report)
        f_feeds = executor.submit(fetch_feeds, settings, report)
        biorxiv = f_biorxiv.result()
        arxiv = f_arxiv.result()
        feeds = f_feeds.result()

    # Only look up abstracts for papers we have not already ingested.
    feeds = _enrich_abstracts([p for p in feeds if p["doi"] not in existing_dois])

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

    for stats in report.values():
        stats.update(new=0, full_abstract=0)
    for paper in seen_dois.values():
        stats = report.get(paper.get("feed"))
        if stats is not None:
            stats["new"] += 1
            stats["full_abstract"] += len(paper["abstract"]) >= SHORT_ABSTRACT_LEN

    pending = load_pending()
    for paper in pending:
        if paper["doi"] not in existing_dois and paper["doi"] not in seen_dois:
            seen_dois[paper["doi"]] = paper

    result = list(seen_dois.values())
    logger.info(
        f"Total after dedup: {len(result)} new papers "
        f"({len(pending)} carried over from earlier failed scoring)"
    )
    return result
