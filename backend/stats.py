"""Analytics over runs: who is where, how long steps take, where people drop off."""

from __future__ import annotations

from datetime import datetime, timezone
from statistics import median
from typing import Any, Dict, List, Optional

from backend import bpmn
from backend.models import Run, RunStatus, Track


def _ts(s: Optional[str]) -> datetime:
    return datetime.fromisoformat(s) if s else datetime.now(timezone.utc)


def _secs(a: Optional[str], b: Optional[str]) -> float:
    return max((_ts(b) - _ts(a)).total_seconds(), 0.0)


def _agg(values: List[float]) -> Dict[str, Optional[float]]:
    if not values:
        return {"avg": None, "median": None, "max": None}
    return {"avg": sum(values) / len(values), "median": median(values), "max": max(values)}


def run_row(run: Run, track: Optional[Track], stale_hours: int) -> Dict[str, Any]:
    title = track.step_name(run.current_step) if track and run.current_step else None
    on_step = _secs(run.visits[-1].entered_at, None) if run.visits and run.status == RunStatus.active else None
    idle = _secs(run.updated_at, None)
    status = run.status.value
    if run.status == RunStatus.active and idle > stale_hours * 3600:
        status = "stalled"
    done = len({v.step_id for v in run.visits} - {run.current_step})
    if run.status == RunStatus.completed:
        progress = 100
    elif track and run.current_step in track.graph.nodes:
        progress = round(100 * done / (done + 1 + bpmn.remaining(track.graph, run.current_step)))
    else:
        progress = 0
    return {
        "run_id": run.run_id, "user_id": run.user_id, "user_name": run.user_name or run.user_id,
        "track_id": run.track_id, "track_name": run.track_name, "track_version": run.track_version,
        "status": status, "current_step": run.current_step,
        "current_step_title": title,
        "current_group": track.graph.group_of(run.current_step) if track and run.current_step else None,
        "on_step_s": on_step, "idle_s": idle,
        "duration_s": _secs(run.created_at, run.finished_at),
        "progress": progress,
        "jira": [i.key for i in run.jira_issues],
        "created_at": run.created_at, "updated_at": run.updated_at,
        "finished_at": run.finished_at,
    }


def track_stats(track: Track, runs: List[Run]) -> Dict[str, Any]:
    runs = [r for r in runs if r.track_id == track.id]
    done = [r for r in runs if r.status == RunStatus.completed]
    steps = []
    for sid in bpmn.order(track.graph):
        durations = [_secs(v.entered_at, v.left_at) for r in runs for v in r.visits
                     if v.step_id == sid and v.left_at]
        reached = sum(1 for r in runs if any(v.step_id == sid for v in r.visits))
        now_here = [r for r in runs if r.status == RunStatus.active and r.current_step == sid]
        steps.append({"id": sid, "title": track.step_name(sid),
                      "group": track.graph.group_of(sid), "reached": reached,
                      "passed": len(durations), "active_now": len(now_here),
                      "users_now": [r.user_name or r.user_id for r in now_here],
                      **{f"{k}_s": v for k, v in _agg(durations).items()}})
    return {
        "track_id": track.id, "name": track.name, "status": track.status.value,
        "version": track.version,
        "runs": len(runs),
        "active": sum(1 for r in runs if r.status == RunStatus.active),
        "completed": len(done),
        "cancelled": sum(1 for r in runs if r.status == RunStatus.cancelled),
        "completion_rate": round(100 * len(done) / len(runs)) if runs else None,
        "duration": {f"{k}_s": v for k, v in
                     _agg([_secs(r.created_at, r.finished_at) for r in done]).items()},
        "steps": steps,
    }


def overview(tracks: List[Track], runs: List[Run], stale_hours: int) -> Dict[str, Any]:
    by_id = {t.id: t for t in tracks}
    rows = [run_row(r, by_id.get(r.track_id), stale_hours) for r in runs]
    done = [r for r in runs if r.status == RunStatus.completed]
    return {
        "tracks": len(tracks),
        "published": sum(1 for t in tracks if t.status.value == "published"),
        "runs": len(runs),
        "active": sum(1 for r in rows if r["status"] == "active"),
        "stalled": sum(1 for r in rows if r["status"] == "stalled"),
        "completed": len(done),
        "avg_duration_s": _agg([_secs(r.created_at, r.finished_at) for r in done])["avg"],
        "jira_issues": sum(len(r.jira_issues) for r in runs),
        "in_progress": [r for r in rows if r["status"] in ("active", "stalled")],
        "per_track": [track_stats(t, runs) for t in tracks],
    }
