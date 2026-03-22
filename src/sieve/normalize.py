import html
import re

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_PLACEHOLDER_RE = re.compile(
    r"^(full[\s-]text|abstract\s+available|no\s+abstract|coming\s+soon|see\s+article)",
    re.IGNORECASE,
)
_MIN_ABSTRACT_LEN = 80


def _clean_text(text: str) -> str:
    if not text:
        return text
    text = html.unescape(text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()


def normalize_paper(paper: dict) -> dict:
    """Return a copy of paper with cleaned title and abstract."""
    p = dict(paper)
    p["title"] = _clean_text(p.get("title") or "").replace("\n", " ")
    raw_abstract = _clean_text(p.get("abstract") or "")
    if len(raw_abstract) < _MIN_ABSTRACT_LEN or _PLACEHOLDER_RE.match(raw_abstract):
        raw_abstract = ""
    p["abstract"] = raw_abstract
    return p
