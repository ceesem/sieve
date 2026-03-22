import pathlib
from dataclasses import dataclass, field

import yaml

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent


@dataclass
class FeedConfig:
    name: str
    url: str


@dataclass
class Settings:
    lookback_days: int = 2
    store_threshold: int = 5
    display_threshold: int = 7
    site_threshold: int = 4
    batch_size: int = 30
    sonnet_batch_size: int = 40
    biorxiv_category: str = "neuroscience"
    arxiv_categories: list[str] = field(
        default_factory=lambda: ["q-bio.NC", "q-bio.QM"]
    )
    feeds: list[FeedConfig] = field(default_factory=list)
    max_papers_per_source: int = 200
    mailto: str = ""


def load_settings(path: str | pathlib.Path | None = None) -> Settings:
    if path is None:
        path = PROJECT_ROOT / "config" / "settings.yaml"
    else:
        path = pathlib.Path(path)

    if not path.exists():
        example = path.with_suffix(".yaml.example")
        raise FileNotFoundError(
            f"{path} not found. Copy {example} to {path} and fill in your details."
        )

    with open(path) as f:
        raw = yaml.safe_load(f)

    feeds = [FeedConfig(**fd) for fd in raw.pop("feeds", [])]
    return Settings(**raw, feeds=feeds)
