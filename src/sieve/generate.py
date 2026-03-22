import json
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from . import db
from .settings import PROJECT_ROOT, Settings

TEMPLATE_DIR = Path(__file__).parent / "templates"
SITE_DIR = PROJECT_ROOT / "site"


def build_site(settings: Settings) -> None:
    SITE_DIR.mkdir(parents=True, exist_ok=True)

    papers = db.get_papers_for_display(
        days=settings.lookback_days, site_threshold=settings.site_threshold
    )
    summary = db.get_summary(display_threshold=settings.display_threshold)

    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)
    template = env.get_template("index.html.j2")

    html = template.render(
        papers_json=json.dumps(papers, default=str),
        summary=summary,
        display_threshold=settings.display_threshold,
        generated_at=datetime.now().isoformat(timespec="seconds"),
    )

    (SITE_DIR / "index.html").write_text(html)
