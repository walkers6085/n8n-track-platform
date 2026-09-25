"""FastAPI app: user API (catalog, runs, chat), analyst API (tracks, stats, settings), static UI.

Analyst routes require the X-Analyst-Token header when ANALYST_TOKEN is set in the environment.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from backend import agent, bpmn, engine, jira, links, llm, stats, storage, tracks
from backend.models import (SLUG_RE, MessageIn, Run, RunStatus, Settings, StartRunIn, Track, TrackIn,
                            TrackStatus)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="Track Platform", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def analyst_auth(x_analyst_token: Optional[str] = Header(default=None),
                 token_q: Optional[str] = Query(default=None, alias="token")) -> None:
    """Header for fetch() calls; ?token= for plain download links (export)."""
    token = os.environ.get("ANALYST_TOKEN")
    if token and token not in (x_analyst_token, token_q):
        raise HTTPException(401, "нужен токен аналитика")


user_api = APIRouter(prefix="/api")
analyst_api = APIRouter(prefix="/api", dependencies=[Depends(analyst_auth)])


def _load_track(track_id: str, version: Optional[int] = None) -> Track:
    try:
        return storage.load_track(track_id, version)
    except FileNotFoundError:
        raise HTTPException(404, "трек не найден")
    except ValueError as e:
        raise HTTPException(400, str(e))


def _load_run(run_id: str) -> Run:
    try:
        return storage.load_run(run_id)
    except FileNotFoundError:
        raise HTTPException(404, "прохождение не найдено")
    except ValueError as e:
        raise HTTPException(400, str(e))


def _rows(runs: List[Run]) -> List[Dict[str, Any]]:
    """Table rows, each computed against the track version the run is pinned to."""
    stale = storage.load_settings().runs.stale_hours
    cache: Dict[tuple, Optional[Track]] = {}
    out = []
    for r in runs:
        key = (r.track_id, r.track_version)
        if key not in cache:
            try:
                cache[key] = engine.track_for_run(r)
            except FileNotFoundError:
                cache[key] = None
        out.append(stats.run_row(r, cache[key], stale))
    return out


def _run_view(run: Run) -> Dict[str, Any]:
    track = engine.track_for_run(run)
    return {"run": run.model_dump(mode="json"), "progress": engine.progress(run, track)}


@user_api.get("/health")
def health():
    return {"status": "ok", "version": app.version}


# ---------------------------------------------------------------------------
# User: catalog and runs
# ---------------------------------------------------------------------------

@user_api.get("/catalog")
def catalog():
    return [{"id": t.id, "name": t.name, "description": t.description,
             "steps": len(t.graph.tasks), "version": t.version}
            for t in storage.list_tracks(TrackStatus.published)]


@user_api.post("/runs", status_code=201)
def start_run(body: StartRunIn):
    track = _load_track(body.track_id)
    try:
        run = engine.start_run(track, body.user_id, body.user_name)
    except engine.RuleError as e:
        raise HTTPException(400, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    with storage.run_lock(run.run_id):
        agent.run_turn(run, None, storage.load_settings())
        storage.save_run(run)
    return _run_view(run)


@user_api.get("/runs")
def list_user_runs(user_id: str = Query(..., min_length=1)):
    return _rows(storage.list_runs(user_id=user_id))


@user_api.get("/runs/{run_id}")
def get_run(run_id: str):
    return _run_view(_load_run(run_id))


@user_api.post("/runs/{run_id}/messages")
def post_message(run_id: str, body: MessageIn):
    lock = storage.run_lock(run_id)
    if not lock.acquire(timeout=1):
        raise HTTPException(409, "агент ещё отвечает на предыдущее сообщение")
    try:
        run = _load_run(run_id)
        try:
            agent.run_turn(run, body.text.strip(), storage.load_settings())
        except engine.RuleError as e:
            raise HTTPException(400, str(e))
        storage.save_run(run)
        return _run_view(run)
    finally:
        lock.release()


@user_api.post("/runs/{run_id}/resume")
def resume_run(run_id: str):
    """Ask the agent to recap where the user stopped (used when coming back to a run)."""
    with storage.run_lock(run_id):
        run = _load_run(run_id)
        if run.status == RunStatus.active:
            agent.run_turn(run, None, storage.load_settings())
            storage.save_run(run)
        return _run_view(run)


@user_api.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str):
    with storage.run_lock(run_id):
        run = _load_run(run_id)
        try:
            engine.cancel(run)
        except engine.RuleError as e:
            raise HTTPException(400, str(e))
        storage.save_run(run)
        return _run_view(run)


# ---------------------------------------------------------------------------
# Analyst: tracks
# ---------------------------------------------------------------------------

@analyst_api.get("/tracks")
def list_tracks():
    runs = storage.list_runs()
    out = []
    for t in storage.list_tracks():
        mine = [r for r in runs if r.track_id == t.id]
        out.append({"id": t.id, "name": t.name, "description": t.description,
                    "status": t.status.value, "version": t.version, "steps": len(t.graph.tasks),
                    "updated_at": t.updated_at, "runs": len(mine),
                    "active": sum(1 for r in mine if r.status == RunStatus.active)})
    return out


def _track_view(t: Track, lint: Optional[Dict[str, List[str]]] = None) -> Dict[str, Any]:
    return {"track": t.model_dump(mode="json"), "lint": lint or tracks.lint(t),
            "versions": storage.list_versions(t.id) if _exists(t.id) else []}


def _exists(track_id: str) -> bool:
    try:
        storage.load_track(track_id)
        return True
    except FileNotFoundError:
        return False


@analyst_api.get("/tracks/{track_id}")
def get_track(track_id: str, version: Optional[int] = None):
    return _track_view(_load_track(track_id, version))


def _build(body: TrackIn, existing: Optional[Track]) -> Track:
    """Editor payload -> consistent track (scheme is the source of truth for the steps)."""
    try:
        meta = existing.meta().model_dump() if existing else {}
        meta.update(id=body.id, name=body.name, description=body.description,
                    agent_instructions=body.agent_instructions, model=body.model)
        if not body.bpmn.strip():
            if existing:
                raise HTTPException(400, "пустая схема")
            base = tracks.new_track(body.id, body.name, body.description)
            return base.model_copy(update={k: v for k, v in meta.items() if k != "track_uuid"})
        t = Track.model_validate({**meta, "bpmn": body.bpmn,
                                  "tasks": body.tasks or (existing.tasks if existing else []),
                                  "activities": body.activities})
        return tracks.sync(t, body.task_types)
    except ValidationError as e:
        raise HTTPException(422, "; ".join(str(err["msg"]) for err in e.errors()))
    except bpmn.BpmnError as e:
        raise HTTPException(422, str(e))


def _save(track: Track, status: TrackStatus) -> Dict[str, Any]:
    track = track.model_copy(update={"status": status})
    lint = tracks.lint(track)
    if status == TrackStatus.published and lint["errors"]:
        raise HTTPException(400, "опубликованный трек должен быть без ошибок: " + "; ".join(lint["errors"]))
    return _track_view(storage.save_track(track), lint)


@analyst_api.post("/tracks", status_code=201)
def create_track(body: TrackIn):
    if not SLUG_RE.match(body.id or ""):
        raise HTTPException(400, "идентификатор: латиница, цифры, - и _")
    if _exists(body.id):
        raise HTTPException(409, f"трек с id «{body.id}» уже есть")
    return _save(_build(body, None), TrackStatus.draft)


@analyst_api.put("/tracks/{track_id}")
def update_track(track_id: str, body: TrackIn):
    if body.id != track_id:
        raise HTTPException(400, "id трека менять нельзя, создайте копию")
    existing = _load_track(track_id)
    return _save(_build(body, existing), existing.status)


def _set_status(track_id: str, status: TrackStatus) -> Dict[str, Any]:
    return _save(_load_track(track_id), status)


@analyst_api.post("/tracks/{track_id}/publish")
def publish_track(track_id: str):
    return _set_status(track_id, TrackStatus.published)


@analyst_api.post("/tracks/{track_id}/unpublish")
def unpublish_track(track_id: str):
    return _set_status(track_id, TrackStatus.draft)


@analyst_api.post("/tracks/{track_id}/archive")
def archive_track(track_id: str):
    return _set_status(track_id, TrackStatus.archived)


@analyst_api.delete("/tracks/{track_id}")
def delete_track(track_id: str):
    t = _load_track(track_id)
    if t.status == TrackStatus.published:
        raise HTTPException(400, "опубликованный трек нельзя удалить: сначала снимите с публикации "
                                 "или отправьте в архив")
    if any(r.status == RunStatus.active for r in storage.list_runs(track_id=track_id)):
        raise HTTPException(400, "по треку есть незавершённые прохождения, отправьте его в архив")
    storage.delete_track(track_id)
    return {"deleted": track_id}


@analyst_api.post("/tracks/lint")
def lint_track(body: TrackIn):
    try:
        existing = storage.load_track(body.id) if body.id and SLUG_RE.match(body.id) else None
    except FileNotFoundError:
        existing = None
    try:
        return tracks.lint(_build(body, existing))
    except HTTPException as e:
        return {"errors": [str(e.detail)], "warnings": []}


class DraftIn(BaseModel):
    description: str


@analyst_api.post("/tracks/draft")
def draft_track(body: DraftIn):
    """A generated track to open in the editor; it is saved only when the analyst saves it."""
    try:
        t = agent.draft_track(body.description, storage.load_settings())
    except llm.LLMError as e:
        raise HTTPException(502, str(e))
    except (ValueError, ValidationError, KeyError, TypeError) as e:
        raise HTTPException(422, f"модель вернула некорректный трек: {e}")
    return {"track": t.model_dump(mode="json"), "lint": tracks.lint(t), "versions": []}


@analyst_api.post("/tracks/import", status_code=201)
async def import_track(request: Request, id: Optional[str] = None, name: Optional[str] = None,
                       replace: bool = False):
    """Body: a zip with scheme.bpmn, tasks.json and Activity_*/ (the partner system's export)."""
    data = await request.body()
    if not data:
        raise HTTPException(400, "пустой файл")
    if len(data) > 50_000_000:
        raise HTTPException(413, "архив больше 50 МБ")
    try:
        t = tracks.sync(tracks.import_zip(data, id, name))
    except (ValueError, ValidationError, bpmn.BpmnError) as e:
        raise HTTPException(422, str(e))
    if not SLUG_RE.match(t.id):
        raise HTTPException(400, f"некорректный id трека «{t.id}»")
    if _exists(t.id):
        if not replace:
            raise HTTPException(409, f"трек «{t.id}» уже есть — импортируйте с заменой или под другим id")
        old = storage.load_track(t.id)
        t = t.model_copy(update={"status": old.status, "track_uuid": old.track_uuid,
                                 "created_at": old.created_at})
        return _save(t, old.status)
    return _save(t, TrackStatus.draft)


@analyst_api.get("/tracks/{track_id}/export")
def export_track(track_id: str, version: Optional[int] = None, pure: bool = False):
    t = _load_track(track_id, version)
    data = tracks.export_zip(t, pure=pure)
    fname = f"{t.id}-v{t.version}{'-partner' if pure else ''}.zip"
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# ---------------------------------------------------------------------------
# Analyst: runs and stats
# ---------------------------------------------------------------------------

@analyst_api.get("/stats/overview")
def stats_overview():
    return stats.overview(storage.list_tracks(), storage.list_runs(),
                          storage.load_settings().runs.stale_hours)


@analyst_api.get("/stats/tracks/{track_id}")
def stats_track(track_id: str):
    return stats.track_stats(_load_track(track_id), storage.list_runs(track_id=track_id))


@analyst_api.get("/admin/runs")
def admin_runs(track_id: Optional[str] = None, status: Optional[str] = None):
    rows = _rows(storage.list_runs(track_id=track_id))
    return [r for r in rows if not status or r["status"] == status]


@analyst_api.get("/admin/runs/{run_id}")
def admin_run(run_id: str):
    run = _load_run(run_id)
    view = _run_view(run)
    view["row"] = stats.run_row(run, engine.track_for_run(run),
                                storage.load_settings().runs.stale_hours)
    return view


# ---------------------------------------------------------------------------
# Analyst: settings
# ---------------------------------------------------------------------------

@analyst_api.get("/settings")
def get_settings():
    return storage.public_settings(storage.load_settings())


@analyst_api.put("/settings")
def put_settings(body: Settings):
    return storage.public_settings(storage.save_settings(body))


@analyst_api.post("/settings/test-agent")
def test_agent():
    try:
        msg = llm.chat(storage.load_settings().agent,
                       [{"role": "user", "content": "Ответь одним словом: готов?"}])
        return {"ok": True, "message": (msg.get("content") or "").strip()[:200]}
    except llm.LLMError as e:
        return {"ok": False, "message": str(e)}


@analyst_api.post("/settings/test-jira")
def test_jira():
    try:
        return {"ok": True, "message": jira.check_connection(storage.load_settings().jira)}
    except Exception as e:
        return {"ok": False, "message": str(e)}


class LinkIn(BaseModel):
    url: str


@analyst_api.post("/links/preview")
def preview_link(body: LinkIn):
    """Show the analyst exactly what the agent will read from a link."""
    if not body.url.startswith(("http://", "https://")):
        raise HTTPException(400, "нужна ссылка http(s)")
    try:
        text = links.fetch_text(body.url, storage.load_settings().links)
        return {"ok": True, "chars": len(text), "text": text[:4000]}
    except Exception as e:
        return {"ok": False, "chars": 0, "text": str(e)}


app.include_router(user_api)
app.include_router(analyst_api)

if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=19001)
