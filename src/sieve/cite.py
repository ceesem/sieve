import logging
import os
import re
import time

import httpx

from .db import get_existing_dois, mark_unseen_bulk

logger = logging.getLogger(__name__)

S2_BASE = "https://api.semanticscholar.org/graph/v1/paper"
S2_REC_BASE = "https://api.semanticscholar.org/recommendations/v1/papers/forpaper"
S2_FIELDS = "title,abstract,authors,year,externalIds,publicationVenue"
S2_PAGE_LIMIT = 500

OA_BASE = "https://api.openalex.org/works"
OA_SELECT = (
    "doi,title,abstract_inverted_index,authorships,publication_year,primary_location"
)
OA_BATCH = 50

_S2_ID_RE = re.compile(r"[0-9a-f]{40}")
_S2_URL_RE = re.compile(r"semanticscholar\.org/paper/[^/]+/([0-9a-f]{40})")


# ---------------------------------------------------------------------------
# Semantic Scholar helpers
# ---------------------------------------------------------------------------


def _s2_headers() -> dict:
    key = os.environ.get("S2_API_KEY", "")
    return {"x-api-key": key} if key else {}


def _extract_paper_id(identifier: str) -> str | None:
    """Return an S2-ready paper ID if the identifier is already one, bypassing DOI lookup."""
    m = _S2_URL_RE.search(identifier)
    if m:
        return m.group(1)
    stripped = identifier.strip()
    if _S2_ID_RE.fullmatch(stripped):
        return stripped
    if stripped.lower().startswith("corpusid:"):
        return "CorpusID:" + stripped[len("corpusid:") :]
    if stripped.isdigit():
        return "CorpusID:" + stripped
    return None


def _get_s2_paper_id(client: httpx.Client, doi: str) -> str | None:
    try:
        resp = client.get(
            f"{S2_BASE}/{doi}", params={"fields": "paperId"}, headers=_s2_headers()
        )
        if resp.status_code == 200:
            return resp.json().get("paperId")
        logger.error(f"S2 paper lookup failed ({resp.status_code}) for DOI {doi}")
    except Exception as e:
        logger.error(f"S2 paper lookup error for DOI {doi}: {e}")
    return None


def _fetch_s2_page(
    client: httpx.Client, url: str, offset: int
) -> tuple[list[dict], bool]:
    """Returns (items, publisher_blocked)."""
    params = {"fields": S2_FIELDS, "limit": S2_PAGE_LIMIT, "offset": offset}
    for attempt in range(3):
        try:
            resp = client.get(url, params=params, headers=_s2_headers())
            if resp.status_code == 429:
                wait = 5 * (attempt + 1)
                logger.warning(f"S2 rate limited — waiting {wait}s before retry")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            body = resp.json()
            data = body.get("data") or []
            if not data and offset == 0:
                disclaimer = (
                    (body.get("citingPaperInfo") or {})
                    .get("openAccessPdf", {})
                    .get("disclaimer", "")
                )
                if "elided by the publisher" in disclaimer:
                    return [], True
            return data, False
        except Exception as e:
            logger.error(f"S2 fetch error at offset {offset}: {e}")
            return [], False
    logger.error(f"S2 fetch failed after retries (429) for offset {offset}")
    return [], False


def _fetch_s2_recommendations(client: httpx.Client, paper_id: str) -> list[dict]:
    try:
        resp = client.get(
            f"{S2_REC_BASE}/{paper_id}",
            params={"fields": S2_FIELDS, "limit": 500},
            headers=_s2_headers(),
        )
        if resp.status_code == 429:
            logger.warning("S2 rate limited on recommendations — waiting 5s")
            time.sleep(5)
            resp = client.get(
                f"{S2_REC_BASE}/{paper_id}",
                params={"fields": S2_FIELDS, "limit": 500},
                headers=_s2_headers(),
            )
        if resp.status_code == 200:
            return resp.json().get("recommendedPapers", [])
        logger.error(f"S2 recommendations failed ({resp.status_code}) for {paper_id}")
    except Exception as e:
        logger.error(f"S2 recommendations error: {e}")
    return []


def _normalize_s2_paper(item: dict) -> dict | None:
    p = item.get("citedPaper") or item.get("citingPaper") or item
    ext = p.get("externalIds") or {}
    doi = ext.get("DOI")
    if not doi:
        arxiv_id = ext.get("ArXiv")
        if arxiv_id:
            doi = f"arxiv:{arxiv_id}"
        else:
            return None
    year = p.get("year")
    authors = [a.get("name", "") for a in (p.get("authors") or []) if a.get("name")]
    journal = (p.get("publicationVenue") or {}).get("name") or ""
    return {
        "doi": doi,
        "title": p.get("title") or "",
        "abstract": p.get("abstract") or "",
        "authors": authors,
        "journal": journal,
        "published_date": f"{year}-01-01" if year else None,
        "source": "cite",
        "url": f"https://doi.org/{doi}"
        if not doi.startswith("arxiv:")
        else f"https://arxiv.org/abs/{doi[6:]}",
    }


# ---------------------------------------------------------------------------
# OpenAlex helpers
# ---------------------------------------------------------------------------


def _reconstruct_abstract(inv_index: dict | None) -> str:
    if not inv_index:
        return ""
    words: dict[int, str] = {}
    for word, positions in inv_index.items():
        for pos in positions:
            words[pos] = word
    return " ".join(words[i] for i in sorted(words))


def _normalize_oa_work(work: dict) -> dict | None:
    raw_doi = work.get("doi") or ""
    doi = raw_doi.replace("https://doi.org/", "").replace("http://doi.org/", "")
    if not doi:
        return None
    year = work.get("publication_year")
    authors = [
        a["author"]["display_name"]
        for a in (work.get("authorships") or [])
        if (a.get("author") or {}).get("display_name")
    ]
    source = (work.get("primary_location") or {}).get("source") or {}
    journal = source.get("display_name", "")
    return {
        "doi": doi,
        "title": work.get("title") or "",
        "abstract": _reconstruct_abstract(work.get("abstract_inverted_index")),
        "authors": authors,
        "journal": journal,
        "published_date": f"{year}-01-01" if year else None,
        "source": "cite",
        "url": f"https://doi.org/{doi}",
    }


def _fetch_openalex_graph(
    client: httpx.Client, doi: str, forward: bool = False, mailto: str = ""
) -> list[dict]:
    """Fetch references (and optionally citations) for a DOI via OpenAlex."""
    base_params = {"mailto": mailto} if mailto else {}

    # Step 1: look up the work to get its OA ID and referenced_works list
    try:
        resp = client.get(
            f"{OA_BASE}/doi:{doi}",
            params={"select": "id,referenced_works", **base_params},
        )
        if resp.status_code != 200:
            logger.error(f"OpenAlex work lookup failed ({resp.status_code}) for {doi}")
            return []
        work_data = resp.json()
    except Exception as e:
        logger.error(f"OpenAlex lookup error: {e}")
        return []

    oa_id = work_data.get("id", "").split("/")[-1]  # e.g. "W2741809807"
    ref_ids = [w.split("/")[-1] for w in (work_data.get("referenced_works") or [])]
    logger.info(f"OpenAlex: {doi} has {len(ref_ids)} references")

    papers: list[dict] = []

    # Step 2: batch-fetch referenced works
    for i in range(0, len(ref_ids), OA_BATCH):
        batch = ref_ids[i : i + OA_BATCH]
        try:
            resp = client.get(
                OA_BASE,
                params={
                    "filter": "openalex_id:" + "|".join(batch),
                    "select": OA_SELECT,
                    "per-page": OA_BATCH,
                    **base_params,
                },
            )
            if resp.status_code == 200:
                for work in resp.json().get("results", []):
                    papers.append(work)
            else:
                logger.warning(f"OpenAlex batch fetch returned {resp.status_code}")
        except Exception as e:
            logger.error(f"OpenAlex batch fetch error: {e}")
        time.sleep(0.1)

    # Step 3: forward citations (papers that cite this work)
    if forward and oa_id:
        cursor = "*"
        cit_count = 0
        while cursor:
            try:
                resp = client.get(
                    OA_BASE,
                    params={
                        "filter": f"cites:{oa_id}",
                        "select": OA_SELECT,
                        "per-page": 200,
                        "cursor": cursor,
                        **base_params,
                    },
                )
                if resp.status_code != 200:
                    break
                body = resp.json()
                results = body.get("results", [])
                papers.extend(results)
                cit_count += len(results)
                cursor = body.get("meta", {}).get("next_cursor")
                if results:
                    time.sleep(0.1)
            except Exception as e:
                logger.error(f"OpenAlex citations fetch error: {e}")
                break
        logger.info(f"OpenAlex: fetched {cit_count} citing papers")

    result = []
    for work in papers:
        p = _normalize_oa_work(work)
        if p:
            result.append(p)
    return result


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def fetch_citation_graph(
    doi: str, forward: bool = False, recommend: bool = False, mailto: str = ""
) -> list[dict]:
    """Fetch citations and/or recommendations for a seed paper.

    Uses Semantic Scholar for references/citations/recommendations; falls back to
    OpenAlex automatically when the publisher has blocked S2 reference access.

    The `doi` argument also accepts a bare S2 paperId, CorpusID, or semanticscholar.org URL.
    """
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if doi.startswith(prefix):
            doi = doi[len(prefix) :]
            break

    client = httpx.Client(timeout=30)
    existing_dois = get_existing_dois()
    all_papers: dict[str, dict] = {}

    already_seen: list[str] = []  # existing DOIs found in the citation graph

    def _add_s2(item: dict) -> None:
        paper = _normalize_s2_paper(item)
        if not paper:
            return
        pdoi = paper["doi"]
        if pdoi in existing_dois:
            already_seen.append(pdoi)
        elif pdoi not in all_papers:
            all_papers[pdoi] = paper

    def _add_oa(work: dict) -> None:
        paper = _normalize_oa_work(work)
        if not paper:
            return
        pdoi = paper["doi"]
        if pdoi in existing_dois:
            already_seen.append(pdoi)
        elif pdoi not in all_papers:
            all_papers[pdoi] = paper

    # Resolve S2 paper ID (needed for recommendations; skip if not a plain DOI)
    paper_id = _extract_paper_id(doi)
    if paper_id:
        logger.info(f"Using S2 paperId directly: {paper_id}")
    else:
        paper_id = _get_s2_paper_id(client, doi)
        if paper_id:
            logger.info(f"Resolved {doi} → S2 paperId {paper_id}")

    # References + citations via S2, with OpenAlex fallback
    s2_blocked = False
    if paper_id:
        directions = [("references", "references")]
        if forward:
            directions.append(("citations", "citations"))

        for endpoint, label in directions:
            url = f"{S2_BASE}/{paper_id}/{endpoint}"
            offset = 0
            page_count = 0
            while True:
                if page_count > 0:
                    time.sleep(0.1)
                data, blocked = _fetch_s2_page(client, url, offset)
                if blocked:
                    logger.warning(
                        f"S2: publisher blocked {label} for this paper — falling back to OpenAlex"
                    )
                    s2_blocked = True
                    break
                if not data:
                    break
                for item in data:
                    _add_s2(item)
                offset += len(data)
                page_count += 1
                if len(data) < S2_PAGE_LIMIT:
                    break
            if not s2_blocked:
                logger.info(f"S2 {label}: fetched {page_count} page(s)")

    if not paper_id or s2_blocked:
        logger.info(f"Fetching via OpenAlex for {doi}")
        oa_papers = _fetch_openalex_graph(client, doi, forward=forward, mailto=mailto)
        for work in oa_papers:
            if work["doi"] not in existing_dois and work["doi"] not in all_papers:
                all_papers[work["doi"]] = work
        logger.info(f"OpenAlex: {len(oa_papers)} papers fetched")

    # Recommendations via S2 (no OA equivalent)
    if recommend and not paper_id:
        logger.info("Skipping recommendations — paper not found in Semantic Scholar")
    if recommend and paper_id:
        recs = _fetch_s2_recommendations(client, paper_id)
        for item in recs:
            _add_s2(item)
        logger.info(f"S2 recommendations: {len(recs)} papers")

    client.close()

    if already_seen:
        n = mark_unseen_bulk(already_seen)
        logger.info(f"Marked {n} already-ingested citation papers as unread")

    result = list(all_papers.values())
    logger.info(f"Citation graph: {len(result)} new papers after deduplication")
    return result
