import json
import shutil
from datetime import datetime
from importlib.metadata import version
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from . import db
from .settings import PROJECT_ROOT, Settings

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
SITE_DIR = PROJECT_ROOT / "site"


def build_site(settings: Settings) -> None:
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    for f in STATIC_DIR.iterdir():
        shutil.copy2(f, SITE_DIR / f.name)

    papers = db.get_papers_for_display(
        days=settings.lookback_days, site_threshold=settings.site_threshold
    )
    summary = db.get_summary(
        display_threshold=settings.display_threshold,
        days=settings.lookback_days,
        site_threshold=settings.site_threshold,
    )

    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)
    template = env.get_template("index.html.j2")

    html = template.render(
        papers_json=json.dumps(papers, default=str),
        summary=summary,
        display_threshold=settings.display_threshold,
        generated_at=datetime.now().isoformat(timespec="seconds"),
        sieve_version=version("sieve"),
    )

    (SITE_DIR / "index.html").write_text(html)


def build_bibliography(
    dois: list[str],
    output_path: Path,
    title: str = "Annotated Bibliography",
    interests_path: Path | None = None,
) -> tuple[int, list[str]]:
    """Render a standalone annotated-bibliography HTML from a list of DOIs.

    If interests_path is provided, papers are re-annotated via Sonnet with
    that custom profile (score + reason overridden; DB not modified).

    Returns (found_count, missing_dois).
    """
    from . import score as _score

    papers: list[dict] = []
    missing: list[str] = []
    for doi in dois:
        p = db.get_paper(doi)
        if p:
            papers.append(p)
        else:
            missing.append(doi)

    if interests_path and papers:
        interests_text = Path(interests_path).read_text()
        annotations = _score.annotate_papers(papers, interests_text)
        for p in papers:
            ann = annotations.get(p["doi"])
            if ann:
                p["score"] = ann.get("score") or p.get("score")
                p["reason"] = ann.get("reason") or p.get("reason")

    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)
    template = env.get_template("bibliography.html.j2")
    html = template.render(
        papers_json=json.dumps(papers, default=str),
        title=title,
        generated_at=datetime.now().isoformat(timespec="seconds"),
        sieve_version=version("sieve"),
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html)
    return len(papers), missing
