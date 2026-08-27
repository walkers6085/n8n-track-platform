#!/usr/bin/env python3
"""FastAPI backend for n8n Track Platform.

Filesystem JSON storage + optional SQLite fallback (not required).
Port 19001, CORS enabled, serves frontend static files.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

try:
    from backend.models import (
        AdvanceRequest,
        CreateTrackRequest,
        HistoryEntry,
        LLMGlobalConfig,
        MessageRequest,
        Run,
        RunStatus,
        StartRunRequest,
        Track,
        TrackSettings,
        TrackStatus,
    )
    from backend import storage
except ImportError:
    from models import (  # type: ignore
        AdvanceRequest,
        CreateTrackRequest,
        HistoryEntry,
        LLMGlobalConfig,
        MessageRequest,
        Run,
        RunStatus,
        StartRunRequest,
        Track,
        TrackSettings,
        TrackStatus,
    )
    import storage  # type: ignore

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(
    title="n8n Track Platform",
    version="1.0.0",
    description="Track orchestration backend - filesystem JSON storage",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
PORT = 19001


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _next_step(track: Track, current_step_id: Optional[str], variables: Dict[str, Any]) -> Optional[str]:
    """Determine next step id based on transitions.

    - If no current step, return first step (if any).
    - Otherwise follow first matching transition.
    - Supports simple condition evaluation: if condition string is present,
      it is treated as truthy only when variables contains matching key.
      For production, keep it simple and deterministic.
    """
    if not track.steps:
        return None
    if current_step_id is None:
        return track.steps[0].id

    # Find transitions from current step
    outgoing = [t for t in track.transitions if t.from_step == current_step_id]
    if not outgoing:
        # Linear fallback: next step in list order
        ids = [s.id for s in track.steps]
        try:
            idx = ids.index(current_step_id)
            if idx + 1 < len(ids):
                return ids[idx + 1]
        except ValueError:
            pass
        return None

    # Evaluate conditions naively: if condition is None or empty -> match.
    # If condition references a variable, check variables dict.
    for t in outgoing:
        if not t.condition:
            return t.to
        # Very simple evaluator: condition like "approved == true" or "variables.x"
        # We do substring check against variables truthiness to stay safe without eval.
        cond = t.condition.strip()
        # Direct variable truthiness: condition == variable name
        if cond in variables and variables[cond]:
            return t.to
        # Handle "var == value" pattern
        if "==" in cond:
            parts = [p.strip().strip("'\"") for p in cond.split("==", 1)]
            var_name = parts[0].removeprefix("variables.")
            expected = parts[1].lower()
            actual = str(variables.get(var_name, "")).lower()
            if actual == expected:
                return t.to
            continue
        if "!=" in cond:
            parts = [p.strip().strip("'\"") for p in cond.split("!=", 1)]
            var_name = parts[0].removeprefix("variables.")
            expected = parts[1].lower()
            actual = str(variables.get(var_name, "")).lower()
            if actual != expected:
                return t.to
            continue
        # Fallback: if condition string appears as key with truthy value
        key = cond.removeprefix("variables.")
        if variables.get(key):
            return t.to

    # No condition matched - follow first unconditional or return None
    return None


def _get_step(track: Track, step_id: str):
    for s in track.steps:
        if s.id == step_id:
            return s
    return None


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"status": "ok", "version": "1.0.0"}


# ---------------------------------------------------------------------------
# Tracks
# ---------------------------------------------------------------------------

@app.post("/api/tracks", response_model=Track, status_code=201)
def create_or_update_track(body: CreateTrackRequest):
    """Create or update a track. Auto-increments version on content change."""
    try:
        # Try to load existing
        try:
            existing = storage.load_track(body.team, body.id)
            # Merge incoming fields onto existing track
            track = Track(
                id=body.id,
                name=body.name,
                team=body.team,
                description=body.description if body.description is not None else existing.description,
                variables=body.variables if body.variables else existing.variables,
                steps=body.steps if body.steps else existing.steps,
                transitions=body.transitions if body.transitions else existing.transitions,
                integrations=body.integrations if body.integrations else existing.integrations,
                settings=body.settings or existing.settings,
                llm_config=body.llm_config or existing.llm_config,
                status=body.status or existing.status,
                version=existing.version,
                created_at=existing.created_at,
                updated_at=existing.updated_at,
            )
            # If steps/transitions explicitly provided (even empty list means keep? handled above)
            # When body.steps is provided as empty list we keep existing per above logic.
            # To allow clearing, client should send non-empty or we treat explicit.
            # Use raw body dict to detect explicit keys
            raw = body.model_dump()
            # For steps/transitions, if caller sent them we should respect even if empty
            # Pydantic always sets default [], so we check by re-parsing request? Simpler: keep above.
            # Actually allow overwrite if caller explicitly wants to replace: use body.steps directly
            # We already handle: if body.steps is truthy use it else keep existing - preserves.
            # For variables/integrations similarly.
            result = storage.save_track(track)
            return result
        except FileNotFoundError:
            # Create new
            track = Track(
                id=body.id,
                name=body.name,
                team=body.team,
                description=body.description,
                variables=body.variables or {},
                steps=body.steps or [],
                transitions=body.transitions or [],
                integrations=body.integrations or {},
                settings=body.settings or TrackSettings(),
                llm_config=body.llm_config,
                status=body.status or TrackStatus.draft,
                version=1,
            )
            result = storage.save_track(track)
            return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("create_or_update_track failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/tracks", response_model=List[Track])
def list_tracks(team: Optional[str] = Query(default=None)):
    try:
        return storage.list_tracks(team=team)
    except Exception as e:
        logger.exception("list_tracks failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/tracks/{team}/{track_id}", response_model=Track)
def get_track(team: str, track_id: str, version: Optional[int] = Query(default=None)):
    try:
        return storage.load_track(team, track_id, version=version)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Track not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/tracks/{team}/{track_id}/versions")
def get_versions(team: str, track_id: str):
    try:
        versions = storage.list_versions(team, track_id)
        # Also include current version info
        track = storage.load_track(team, track_id)
        return {"team": team, "track_id": track_id, "current_version": track.version, "versions": versions}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Track not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/tracks/{team}/{track_id}/publish", response_model=Track)
def publish_track(team: str, track_id: str):
    try:
        track = storage.load_track(team, track_id)
        track.status = TrackStatus.published
        result = storage.save_track(track)
        return result
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Track not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/tracks/{team}/{track_id}")
def delete_track(team: str, track_id: str):
    try:
        storage.delete_track(team, track_id)
        return {"status": "deleted", "team": team, "track_id": track_id}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Track not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

@app.post("/api/runs/start", response_model=Run, status_code=201)
def start_run(body: StartRunRequest):
    try:
        track = storage.load_track(body.team, body.track_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Track not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    run_id = str(uuid.uuid4())
    # Merge track variables with run variables
    variables: Dict[str, Any] = {}
    variables.update(track.variables or {})
    if body.variables:
        variables.update(body.variables)

    # Determine initial step
    initial_step: Optional[str] = None
    if track.steps:
        initial_step = track.steps[0].id

    run = Run(
        run_id=run_id,
        user_id=body.user_id,
        team=body.team,
        track_id=body.track_id,
        track_version=track.version,
        current_step=initial_step,
        status=RunStatus.active if initial_step else RunStatus.completed,
        variables=variables,
        history=[],
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
    )

    # Add system history entry
    run.history.append(HistoryEntry(
        step_id=initial_step,
        type="system",
        text=f"Run started for track {body.track_id} v{track.version}",
        timestamp=datetime.now(timezone.utc).isoformat(),
    ))

    # If initial step is a message-type, add it to history
    if initial_step:
        step = _get_step(track, initial_step)
        if step and step.type.value == "message" and step.config.get("text"):
            run.history.append(HistoryEntry(
                step_id=initial_step,
                type="step_enter",
                text=step.config["text"],
                payload=step.config,
                timestamp=datetime.now(timezone.utc).isoformat(),
            ))

    storage.save_run(run)
    logger.info("Started run %s for %s/%s", run_id, body.team, body.track_id)
    return run


@app.get("/api/runs/{run_id}", response_model=Run)
def get_run(run_id: str, team: Optional[str] = Query(default=None)):
    try:
        if team:
            return storage.load_run(team, run_id)
        return storage.load_run_any(run_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Run not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/runs", response_model=List[Run])
def list_runs(team: Optional[str] = Query(default=None), user_id: Optional[str] = Query(default=None)):
    try:
        return storage.list_runs(team=team, user_id=user_id)
    except Exception as e:
        logger.exception("list_runs failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/runs/{run_id}/message", response_model=Run)
def post_message(run_id: str, body: MessageRequest):
    try:
        run = storage.load_run_any(run_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Run not found")

    if run.status in (RunStatus.completed, RunStatus.failed, RunStatus.cancelled):
        raise HTTPException(status_code=400, detail=f"Run is already {run.status.value}")

    # Append user message to history
    run.history.append(HistoryEntry(
        step_id=run.current_step,
        type="message",
        text=body.text,
        payload=body.payload,
        user_id=body.user_id or run.user_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
    ))

    # Store variable if needed
    run.variables["_last_message"] = body.text
    if body.payload:
        run.variables.update(body.payload)

    # Try to advance workflow
    try:
        track = storage.load_track(run.team, run.track_id, version=run.track_version)
    except FileNotFoundError:
        # Fallback to latest
        try:
            track = storage.load_track(run.team, run.track_id)
        except FileNotFoundError:
            storage.save_run(run)
            return run

    # Determine next step
    next_step = _next_step(track, run.current_step, run.variables)

    if next_step:
        # Record step exit
        run.history.append(HistoryEntry(
            step_id=run.current_step,
            type="step_exit",
            text=f"Transition to {next_step}",
            timestamp=datetime.now(timezone.utc).isoformat(),
        ))
        run.current_step = next_step
        step = _get_step(track, next_step)
        if step:
            # Auto-handle wait / message steps
            if step.type.value in ("message", "wait") and step.config.get("text"):
                run.history.append(HistoryEntry(
                    step_id=next_step,
                    type="step_enter",
                    text=step.config["text"],
                    payload=step.config,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                ))
            elif step.type.value == "wait":
                run.status = RunStatus.waiting
            else:
                run.history.append(HistoryEntry(
                    step_id=next_step,
                    type="step_enter",
                    text=f"Entered step {step.name or step.id} ({step.type.value})",
                    payload=step.config,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                ))
            # If this is the last step and no outgoing transitions, mark waiting or completed
            has_outgoing = any(t.from_step == next_step for t in track.transitions)
            if not has_outgoing:
                # Check if step type suggests completion
                if step.type.value in ("message", "action"):
                    # Stay active, let next message complete or explicit advance
                    pass
    else:
        # No next step -> complete
        run.current_step = None
        run.status = RunStatus.completed
        run.history.append(HistoryEntry(
            type="system",
            text="Run completed",
            timestamp=datetime.now(timezone.utc).isoformat(),
        ))

    storage.save_run(run)
    return run


@app.post("/api/runs/{run_id}/advance", response_model=Run)
def advance_run(run_id: str, body: AdvanceRequest):
    """Webhook endpoint for n8n to advance a run.

    n8n workflows call this to move the run to a specific step or complete it.
    """
    try:
        run = storage.load_run_any(run_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Run not found")

    if run.status in (RunStatus.completed, RunStatus.failed, RunStatus.cancelled):
        raise HTTPException(status_code=400, detail=f"Run is already {run.status.value}")

    if body.variables:
        run.variables.update(body.variables)

    if body.target_step is not None:
        # Explicit step jump
        run.current_step = body.target_step if body.target_step else None
        run.history.append(HistoryEntry(
            step_id=body.target_step,
            type="system",
            text=body.text or f"Advanced to step {body.target_step}" if body.target_step else "Advanced (completed)",
            timestamp=datetime.now(timezone.utc).isoformat(),
        ))
        if not body.target_step:
            run.status = RunStatus.completed
    else:
        # Auto-advance via transitions
        try:
            track = storage.load_track(run.team, run.track_id, version=run.track_version)
        except FileNotFoundError:
            try:
                track = storage.load_track(run.team, run.track_id)
            except FileNotFoundError:
                raise HTTPException(status_code=404, detail="Track not found for run")
        nxt = _next_step(track, run.current_step, run.variables)
        if nxt:
            run.current_step = nxt
            run.history.append(HistoryEntry(
                step_id=nxt,
                type="system",
                text=body.text or f"Advanced to {nxt}",
                timestamp=datetime.now(timezone.utc).isoformat(),
            ))
        else:
            run.current_step = None
            run.status = RunStatus.completed
            run.history.append(HistoryEntry(
                type="system",
                text=body.text or "Run completed via advance",
                timestamp=datetime.now(timezone.utc).isoformat(),
            ))

    if body.status:
        run.status = body.status
        if body.status == RunStatus.completed:
            run.current_step = None

    storage.save_run(run)
    logger.info("Advanced run %s -> step=%s status=%s", run_id, run.current_step, run.status)
    return run


# ---------------------------------------------------------------------------
# Teams / Config
# ---------------------------------------------------------------------------

@app.get("/api/teams")
def get_teams():
    teams = storage.load_teams()
    # Also discover teams from track directories
    try:
        for entry in storage.TRACKS_DIR.iterdir():
            if entry.is_dir():
                name = entry.name
                if not any(t.get("id") == name or t == name for t in teams):
                    teams.append({"id": name, "name": name})
    except FileNotFoundError:
        pass
    return {"teams": teams}


@app.get("/api/config/llm")
def get_llm_config():
    return storage.load_llm_config()


@app.put("/api/config/llm")
def put_llm_config(body: Dict[str, Any]):
    # Validate via model but allow extra fields
    try:
        cfg = LLMGlobalConfig.model_validate(body)
        data = cfg.model_dump(mode="json")
        # Preserve extra keys not in model
        for k, v in body.items():
            if k not in data:
                data["extra"][k] = v
        storage.save_llm_config(data)
        return data
    except Exception as e:
        # Fallback: store raw if validation fails partially
        logger.warning("LLM config validation warning: %s", e)
        # Try minimal validation, else save raw
        try:
            LLMGlobalConfig.model_validate(body)
        except Exception as ve:
            raise HTTPException(status_code=400, detail=str(ve))
        storage.save_llm_config(body)
        return body


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
else:
    logger.warning("Frontend directory not found: %s", FRONTEND_DIR)

    @app.get("/")
    def frontend_missing():
        return JSONResponse({"message": "n8n Track Platform API", "docs": "/docs", "frontend": "not deployed"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
