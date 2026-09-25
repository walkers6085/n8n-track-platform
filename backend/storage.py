"""Filesystem JSON storage.

Layout (under DATA_ROOT, the repo root by default; override with TRACK_PLATFORM_DATA):
  tracks/{track_id}/               the partner system's format, copyable as is:
      scheme.bpmn, tasks.json, Activity_*/{metadata,properties,attachments}.json
      + our sidecars track.json and Activity_*/agent.json (ignored by the partner system)
  track_versions/{track_id}/v{N}/  full immutable snapshots, runs pin one of these
  runs/{run_id}.json
  users/{user_id}.json             facts remembered across runs
  config/settings.json             analyst settings (contains secrets, gitignored)
  config/jira_mock.json            issues created in Jira "mock" mode
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.models import SECRET_FIELDS, SLUG_RE, Activity, Run, Settings, Track, TrackStatus

logger = logging.getLogger(__name__)

_root = Path(os.environ.get("TRACK_PLATFORM_DATA") or Path(__file__).resolve().parent.parent)
_lock = threading.RLock()


def set_root(path: Path) -> None:
    """Point storage at another directory (tests use a temp dir)."""
    global _root
    _root = Path(path)


def root() -> Path:
    return _root


def _dir(name: str) -> Path:
    d = _root / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_id(value: str, label: str) -> str:
    if not value or len(value) > 128 or not SLUG_RE.match(value):
        raise ValueError(f"invalid {label} '{value}'")
    return value


def _read(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Tracks — the partner system's folder layout, see models.Track
# ---------------------------------------------------------------------------

PARTNER_FILES = ("metadata.json", "properties.json", "attachments.json")


def _track_dir(track_id: str) -> Path:
    return _dir("tracks") / _safe_id(track_id, "track id")


def _version_dir(track_id: str, version: int) -> Path:
    return _dir("track_versions") / _safe_id(track_id, "track id") / f"v{int(version)}"


def _compact(data: Any) -> str:
    # the partner system writes one-line JSON with raw UTF-8
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def read_track_dir(d: Path) -> Track:
    """Read a track folder. Works for folders exported by the partner system (no track.json)."""
    from backend import bpmn

    scheme = d / "scheme.bpmn"
    if not scheme.exists():
        raise FileNotFoundError(f"нет {scheme.name} в {d.name}")
    xml = scheme.read_text(encoding="utf-8")
    meta = _read(d / "track.json") if (d / "track.json").exists() else {}
    tasks = _read(d / "tasks.json") if (d / "tasks.json").exists() else []
    activities: Dict[str, Activity] = {}
    for sub in sorted(p for p in d.iterdir() if p.is_dir()):
        if not (sub / "metadata.json").exists():
            continue
        act = {"metadata": _read(sub / "metadata.json")}
        for name, key in (("properties.json", "properties"), ("attachments.json", "attachments"),
                          ("agent.json", "agent")):
            if (sub / name).exists():
                act[key] = _read(sub / name)
        activities[sub.name] = Activity.model_validate(act)
    if not meta:
        g = bpmn.parse(xml)
        track_uuid = next((p.get("trackId") for a in activities.values() for p in a.properties
                           if p.get("trackId")), None)
        meta = {"id": d.name, "name": g.process_name or d.name, "status": "draft"}
        if track_uuid:
            meta["track_uuid"] = track_uuid
    meta["id"] = meta.get("id") or d.name
    return Track.model_validate({**meta, "bpmn": xml, "tasks": tasks, "activities": activities})


def write_track_dir(track: Track, d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "scheme.bpmn").write_text(track.bpmn, encoding="utf-8")
    (d / "tasks.json").write_text(_compact(track.tasks), encoding="utf-8")
    _write(d / "track.json", track.meta().model_dump(mode="json"))
    keep = set(track.activities)
    for tid, act in track.activities.items():
        sub = d / _safe_id(tid, "activity id")
        sub.mkdir(exist_ok=True)
        (sub / "metadata.json").write_text(_compact(act.metadata), encoding="utf-8")
        (sub / "properties.json").write_text(_compact(act.properties), encoding="utf-8")
        (sub / "attachments.json").write_text(_compact(act.attachments), encoding="utf-8")
        agent = act.agent.model_dump(mode="json", exclude_defaults=True)
        if agent:
            _write(sub / "agent.json", agent)
        elif (sub / "agent.json").exists():
            (sub / "agent.json").unlink()
    for sub in d.iterdir():  # activities removed from the scheme
        if sub.is_dir() and sub.name not in keep and (sub / "metadata.json").exists():
            shutil.rmtree(sub)


def load_track(track_id: str, version: Optional[int] = None) -> Track:
    d = _version_dir(track_id, version) if version else _track_dir(track_id)
    if not (d / "scheme.bpmn").exists():
        raise FileNotFoundError(f"track {track_id}" + (f" v{version}" if version else ""))
    return read_track_dir(d)


def _content(track: Track) -> Dict[str, Any]:
    data = track.model_dump(mode="json", exclude={"created_at", "updated_at", "version"})
    data["bpmn"] = track.bpmn.strip()
    return data


def save_track(track: Track) -> Track:
    """Write the track folder and a full snapshot. The version goes up only when content changed."""
    with _lock:
        d = _track_dir(track.id)
        if (d / "scheme.bpmn").exists():
            old = read_track_dir(d)
            if _content(old) == _content(track):
                return old
            track.version = old.version + 1
            track.created_at = old.created_at
        track.touch()
        write_track_dir(track, d)
        snap = _version_dir(track.id, track.version)
        if snap.exists():
            shutil.rmtree(snap)
        write_track_dir(track, snap)
        logger.info("saved track %s v%d", track.id, track.version)
        return read_track_dir(d)


def list_tracks(status: Optional[TrackStatus] = None) -> List[Track]:
    out: List[Track] = []
    for p in sorted(_dir("tracks").glob("*/scheme.bpmn")):
        try:
            t = read_track_dir(p.parent)
        except Exception as e:  # a broken track must not hide the other tracks
            logger.warning("skip %s: %s", p.parent, e)
            continue
        if status is None or t.status == status:
            out.append(t)
    return out


def list_versions(track_id: str) -> List[int]:
    if not (_track_dir(track_id) / "scheme.bpmn").exists():
        raise FileNotFoundError(f"track {track_id}")
    vdir = _dir("track_versions") / _safe_id(track_id, "track id")
    return sorted(int(m.group(1)) for p in (vdir.glob("v*") if vdir.exists() else [])
                  if (m := re.fullmatch(r"v(\d+)", p.name)))


def delete_track(track_id: str) -> None:
    d = _track_dir(track_id)
    if not (d / "scheme.bpmn").exists():
        raise FileNotFoundError(f"track {track_id}")
    shutil.rmtree(d)
    vdir = _dir("track_versions") / _safe_id(track_id, "track id")
    if vdir.exists():
        shutil.rmtree(vdir)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

_run_locks: Dict[str, threading.Lock] = {}


def run_lock(run_id: str) -> threading.Lock:
    """One agent turn at a time per run."""
    with _lock:
        return _run_locks.setdefault(run_id, threading.Lock())


def _run_path(run_id: str) -> Path:
    return _dir("runs") / f"{_safe_id(run_id, 'run id')}.json"


def load_run(run_id: str) -> Run:
    path = _run_path(run_id)
    if not path.exists():
        raise FileNotFoundError(f"run {run_id}")
    return Run.model_validate(_read(path))


def save_run(run: Run) -> Run:
    run.touch()
    _write(_run_path(run.run_id), run.model_dump(mode="json"))
    return run


def list_runs(user_id: Optional[str] = None, track_id: Optional[str] = None) -> List[Run]:
    out: List[Run] = []
    for p in _dir("runs").glob("*.json"):
        try:
            r = Run.model_validate(_read(p))
        except Exception as e:
            logger.warning("skip %s: %s", p, e)
            continue
        if user_id and r.user_id != user_id:
            continue
        if track_id and r.track_id != track_id:
            continue
        out.append(r)
    out.sort(key=lambda r: r.updated_at, reverse=True)
    return out


# ---------------------------------------------------------------------------
# User profile memory
# ---------------------------------------------------------------------------

def _user_path(user_id: str) -> Path:
    return _dir("users") / f"{_safe_id(user_id, 'user id')}.json"


def load_profile(user_id: str) -> Dict[str, Any]:
    path = _user_path(user_id)
    return _read(path) if path.exists() else {}


def update_profile(user_id: str, facts: Dict[str, Any]) -> Dict[str, Any]:
    with _lock:
        data = load_profile(user_id)
        data.update(facts)
        _write(_user_path(user_id), data)
        return data


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _settings_path() -> Path:
    return _dir("config") / "settings.json"


def load_settings() -> Settings:
    path = _settings_path()
    s = Settings.model_validate(_read(path)) if path.exists() else Settings()
    if not s.agent.api_key:
        s.agent.api_key = os.environ.get("AGENT_API_KEY") or os.environ.get("MISTRAL_API_KEY", "")
    return s


def save_settings(new: Settings) -> Settings:
    """Secrets sent back empty or masked keep their stored value."""
    with _lock:
        path = _settings_path()
        old = Settings.model_validate(_read(path)) if path.exists() else Settings()
        for section, field in SECRET_FIELDS:
            val = getattr(getattr(new, section), field)
            if not val or set(val) == {"•"}:
                setattr(getattr(new, section), field, getattr(getattr(old, section), field))
        _write(path, new.model_dump(mode="json"))
        return new


def public_settings(s: Settings) -> Dict[str, Any]:
    data = s.model_dump(mode="json")
    for section, field in SECRET_FIELDS:
        data[section][f"{field}_set"] = bool(data[section][field])
        data[section][field] = "••••••••" if data[section][field] else ""
    return data


def load_json(name: str, default: Any) -> Any:
    path = _dir("config") / name
    return _read(path) if path.exists() else default


def save_json(name: str, data: Any) -> None:
    _write(_dir("config") / name, data)
