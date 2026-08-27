"""Filesystem storage for tracks and runs.

Layout:
  tracks/{team}/{track_id}/track.json        -- latest version
  tracks/{team}/{track_id}/v{N}.json         -- version snapshots
  tracks/{team}/{track_id}/versions.json     -- version index (optional)
  runs/{team}/{run_id}.json
  config/llm.json
  config/teams.json
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from backend.models import Run, Track
except ImportError:
    from models import Run, Track  # type: ignore

logger = logging.getLogger(__name__)

# Base directories - resolved relative to project root
BASE_DIR = Path(__file__).resolve().parent.parent
TRACKS_DIR = BASE_DIR / "tracks"
RUNS_DIR = BASE_DIR / "runs"
CONFIG_DIR = BASE_DIR / "config"

_SLUG_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _ensure_dirs() -> None:
    TRACKS_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def _validate_slug(value: str, label: str = "identifier") -> None:
    if not _SLUG_RE.match(value):
        raise ValueError(f"Invalid {label} '{value}': must match ^[a-zA-Z0-9_-]+$")


def _track_dir(team: str, track_id: str) -> Path:
    _validate_slug(team, "team")
    _validate_slug(track_id, "track_id")
    return TRACKS_DIR / team / track_id


def _track_file(team: str, track_id: str) -> Path:
    return _track_dir(team, track_id) / "track.json"


def _version_file(team: str, track_id: str, version: int) -> Path:
    return _track_dir(team, track_id) / f"v{version}.json"


def _run_file(team: str, run_id: str) -> Path:
    # run_id may contain broader chars but must not allow path traversal
    if "/" in run_id or "\\" in run_id or ".." in run_id:
        raise ValueError(f"Invalid run_id '{run_id}'")
    return RUNS_DIR / team / f"{run_id}.json"


# ---------------------------------------------------------------------------
# Track operations
# ---------------------------------------------------------------------------

def load_track(team: str, track_id: str, version: Optional[int] = None) -> Track:
    """Load a track. If version is None, loads latest (track.json)."""
    _ensure_dirs()
    if version is not None:
        path = _version_file(team, track_id, version)
    else:
        path = _track_file(team, track_id)
    if not path.exists():
        raise FileNotFoundError(f"Track not found: {team}/{track_id}" + (f" v{version}" if version else ""))
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return Track.model_validate(data)


def save_track(track: Track) -> Track:
    """Save track to filesystem. Creates version snapshot.

    If the track already exists and content changed, increments version.
    Caller may also set track.version explicitly.
    """
    _ensure_dirs()
    tdir = _track_dir(track.team, track.id)
    tdir.mkdir(parents=True, exist_ok=True)

    track.touch()

    # Determine if we need to bump version: if file exists, compare version
    dest = _track_file(track.team, track.id)
    if dest.exists():
        try:
            existing = Track.model_validate(json.loads(dest.read_text(encoding="utf-8")))
            # If caller did not bump version, auto-increment when content differs
            if track.version <= existing.version:
                # Only bump if data actually changed (avoid spurious bumps)
                old = existing.model_dump(exclude={"updated_at", "created_at"})
                new = track.model_dump(exclude={"updated_at", "created_at"})
                if old != new:
                    track.version = existing.version + 1
                else:
                    track.version = existing.version
        except Exception:
            # If existing file is corrupt, overwrite
            pass

    # Write latest
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(track.model_dump(mode="json"), f, indent=2, ensure_ascii=False)

    # Write version snapshot
    vpath = _version_file(track.team, track.id, track.version)
    with open(vpath, "w", encoding="utf-8") as f:
        json.dump(track.model_dump(mode="json"), f, indent=2, ensure_ascii=False)

    logger.info("Saved track %s/%s v%d", track.team, track.id, track.version)
    return track


def create_version(team: str, track_id: str) -> Track:
    """Explicitly snapshot current track.json into next version file."""
    track = load_track(team, track_id)
    next_version = track.version + 1
    track.version = next_version
    track.touch()
    # update latest
    dest = _track_file(team, track_id)
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(track.model_dump(mode="json"), f, indent=2, ensure_ascii=False)
    vpath = _version_file(team, track_id, next_version)
    with open(vpath, "w", encoding="utf-8") as f:
        json.dump(track.model_dump(mode="json"), f, indent=2, ensure_ascii=False)
    logger.info("Created version %d for %s/%s", next_version, team, track_id)
    return track


def list_tracks(team: Optional[str] = None) -> List[Track]:
    """List all tracks, optionally filtered by team."""
    _ensure_dirs()
    result: List[Track] = []
    if team:
        pattern = str(TRACKS_DIR / team / "*" / "track.json")
    else:
        pattern = str(TRACKS_DIR / "*" / "*" / "track.json")
    for path in glob.glob(pattern):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            result.append(Track.model_validate(data))
        except Exception as e:
            logger.warning("Skipping invalid track file %s: %s", path, e)
    # Sort by team, id
    result.sort(key=lambda t: (t.team, t.id))
    return result


def list_versions(team: str, track_id: str) -> List[int]:
    """Return sorted list of available version numbers for a track."""
    _ensure_dirs()
    tdir = _track_dir(team, track_id)
    if not tdir.exists():
        raise FileNotFoundError(f"Track not found: {team}/{track_id}")
    versions: List[int] = []
    for p in tdir.glob("v*.json"):
        m = re.match(r"v(\d+)\.json", p.name)
        if m:
            versions.append(int(m.group(1)))
    versions.sort()
    return versions


def delete_track(team: str, track_id: str) -> None:
    tdir = _track_dir(team, track_id)
    if not tdir.exists():
        raise FileNotFoundError(f"Track not found: {team}/{track_id}")
    shutil.rmtree(tdir)
    logger.info("Deleted track %s/%s", team, track_id)


# ---------------------------------------------------------------------------
# Run operations
# ---------------------------------------------------------------------------

def load_run(team: str, run_id: str) -> Run:
    _ensure_dirs()
    # Try team-specific path first, then search all teams
    path = _run_file(team, run_id)
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return Run.model_validate(data)
    # Fallback: search under runs/*/
    for candidate in RUNS_DIR.glob(f"*/{run_id}.json"):
        with open(candidate, "r", encoding="utf-8") as f:
            data = json.load(f)
        return Run.model_validate(data)
    raise FileNotFoundError(f"Run not found: {run_id}")


def load_run_any(run_id: str) -> Run:
    """Load run without knowing team."""
    _ensure_dirs()
    for candidate in RUNS_DIR.glob(f"*/{run_id}.json"):
        with open(candidate, "r", encoding="utf-8") as f:
            data = json.load(f)
        return Run.model_validate(data)
    # Also try flat
    flat = RUNS_DIR / f"{run_id}.json"
    if flat.exists():
        with open(flat, "r", encoding="utf-8") as f:
            data = json.load(f)
        return Run.model_validate(data)
    raise FileNotFoundError(f"Run not found: {run_id}")


def save_run(run: Run) -> Run:
    _ensure_dirs()
    run.touch()
    path = _run_file(run.team, run.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(run.model_dump(mode="json"), f, indent=2, ensure_ascii=False)
    logger.info("Saved run %s team=%s status=%s", run.run_id, run.team, run.status)
    return run


def list_runs(team: Optional[str] = None, user_id: Optional[str] = None) -> List[Run]:
    _ensure_dirs()
    result: List[Run] = []
    if team:
        pattern = str(RUNS_DIR / team / "*.json")
    else:
        pattern = str(RUNS_DIR / "*" / "*.json")
        # also include flat files
        for p in RUNS_DIR.glob("*.json"):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                r = Run.model_validate(data)
                if user_id and r.user_id != user_id:
                    continue
                result.append(r)
            except Exception as e:
                logger.warning("Skipping invalid run file %s: %s", p, e)
    for path in glob.glob(pattern):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            r = Run.model_validate(data)
            if user_id and r.user_id != user_id:
                continue
            result.append(r)
        except Exception as e:
            logger.warning("Skipping invalid run file %s: %s", path, e)
    # Most recent first
    result.sort(key=lambda r: r.created_at, reverse=True)
    return result


# ---------------------------------------------------------------------------
# Config operations
# ---------------------------------------------------------------------------

def load_llm_config() -> Dict[str, Any]:
    _ensure_dirs()
    path = CONFIG_DIR / "llm.json"
    if not path.exists():
        return {
            "provider": "mistral",
            "model": "mistral-small-latest",
            "temperature": 0.7,
            "api_key_configured": False,
            "base_url": None,
            "max_tokens": None,
            "extra": {},
        }
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_llm_config(data: Dict[str, Any]) -> Dict[str, Any]:
    _ensure_dirs()
    path = CONFIG_DIR / "llm.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return data


def load_teams() -> List[Dict[str, Any]]:
    _ensure_dirs()
    path = CONFIG_DIR / "teams.json"
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Normalize: support both list and dict formats
    if isinstance(data, dict) and "teams" in data:
        return data["teams"]
    if isinstance(data, list):
        return data
    return []


def save_teams(data: Any) -> Any:
    _ensure_dirs()
    path = CONFIG_DIR / "teams.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return data
