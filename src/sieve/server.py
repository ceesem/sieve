import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import db
from .generate import SITE_DIR, build_site
from .settings import load_settings

app = FastAPI()
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


class DoiBody(BaseModel):
    doi: str


class NoteBody(BaseModel):
    doi: str
    note: str


@app.get("/")
def index():
    return FileResponse(str(SITE_DIR / "index.html"))


@app.get("/favicon.svg")
def favicon_svg():
    return FileResponse(str(SITE_DIR / "favicon.svg"), media_type="image/svg+xml")


@app.get("/favicon.png")
def favicon_png():
    return FileResponse(str(SITE_DIR / "favicon.png"), media_type="image/png")


@app.get("/favicon.ico")
def favicon_ico():
    return FileResponse(str(SITE_DIR / "favicon.ico"), media_type="image/x-icon")


@app.post("/mark-seen")
def mark_seen(body: DoiBody):
    try:
        db.mark_seen(body.doi)
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.post("/toggle-seen")
def toggle_seen(body: DoiBody):
    try:
        new_val = db.toggle_seen(body.doi)
        return {"status": "ok", "seen": new_val}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.post("/toggle-rl-read")
def toggle_rl_read(body: DoiBody):
    try:
        new_val = db.toggle_rl_read(body.doi)
        return {"status": "ok", "rl_read": new_val}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.post("/mark-all-rl-read")
def mark_all_rl_read():
    try:
        count = db.mark_all_rl_read()
        return {"status": "ok", "count": count}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.post("/toggle-reading-list")
def toggle_reading_list(body: DoiBody):
    try:
        db.toggle_reading_list(body.doi)
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.post("/set-note")
def set_note(body: NoteBody):
    try:
        db.set_note(body.doi, body.note)
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.post("/mark-all-seen")
def mark_all_seen():
    try:
        count = db.mark_all_seen()
        return {"status": "ok", "count": count}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _parse_creator(name: str) -> dict:
    name = name.strip()
    if "," in name:
        last, _, first = name.partition(",")
        return {
            "creatorType": "author",
            "firstName": first.strip(),
            "lastName": last.strip(),
        }
    parts = name.split()
    if len(parts) == 1:
        return {"creatorType": "author", "firstName": "", "lastName": parts[0]}
    return {
        "creatorType": "author",
        "firstName": " ".join(parts[:-1]),
        "lastName": parts[-1],
    }


@app.post("/send-to-zotero")
def send_to_zotero(body: DoiBody):
    paper = db.get_paper(body.doi)
    if paper is None:
        raise HTTPException(status_code=404, detail="Paper not found")

    item_type = (
        "preprint" if paper.get("source") in ("biorxiv", "arxiv") else "journalArticle"
    )
    url = paper.get("url") or (
        f"https://doi.org/{paper['doi']}"
        if paper.get("doi") and not paper["doi"].startswith("arxiv:")
        else ""
    )
    item = {
        "itemType": item_type,
        "title": paper.get("title", ""),
        "creators": [_parse_creator(a) for a in (paper.get("authors") or [])],
        "abstractNote": paper.get("abstract") or "",
        "publicationTitle": paper.get("journal") or "",
        "date": paper.get("published_date") or "",
        "url": url,
        "tags": [],
    }
    if paper.get("doi") and not paper["doi"].startswith("arxiv:"):
        item["DOI"] = paper["doi"]

    try:
        r = httpx.post(
            "http://localhost:23119/connector/saveItems",
            json={"items": [item], "uri": url},
            headers={"Content-Type": "application/json"},
            timeout=5.0,
        )
        r.raise_for_status()
        return {"status": "ok"}
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="Zotero is not running")
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/regenerate")
def regenerate():
    try:
        settings = load_settings()
        build_site(settings)
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}
