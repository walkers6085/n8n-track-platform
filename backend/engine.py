"""Run lifecycle over a BPMN scheme. The server — not the model — decides whether a step may be
left and which step comes next (gateway conditions are formal expressions over properties)."""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional, Tuple

from backend import bpmn, links, storage
from backend.models import (ChatMessage, Link, Run, RunStatus, StepVisit, Track, TrackStatus,
                            now_iso)


class RuleError(Exception):
    """A business rule forbids the action; the message is shown to the user/agent as is."""


def track_for_run(run: Run) -> Track:
    """The version pinned at start; the latest one only if that snapshot vanished."""
    try:
        return storage.load_track(run.track_id, version=run.track_version)
    except FileNotFoundError:
        return storage.load_track(run.track_id)


def event(run: Run, text: str) -> None:
    run.messages.append(ChatMessage(role="event", text=text, step_id=run.current_step))


def is_filled(value: Any) -> bool:
    return value not in (None, "", [], {})


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

def variants(prop: Dict[str, Any]) -> List[Tuple[Any, str]]:
    """valueVariants as (value, label). Accepts strings or objects with value/code/name/label."""
    out = []
    for v in prop.get("valueVariants") or []:
        if isinstance(v, dict):
            val = next((v[k] for k in ("value", "code", "codeName", "id", "name") if k in v), None)
            label = next((v[k] for k in ("label", "name", "title", "value") if k in v), val)
            out.append((val, str(label)))
        else:
            out.append((v, str(v)))
    return out


def coerce(prop: Optional[Dict[str, Any]], value: Any) -> Tuple[Any, Optional[str]]:
    """Convert what the user said into the property's type. Returns (value, error)."""
    if prop is None or value is None:
        return value, None
    vtype = str(prop.get("valueType") or "string").lower()
    vs = variants(prop)
    if vs:
        s = str(value).strip().lower()
        for val, label in vs:
            if s in (str(val).strip().lower(), label.strip().lower()):
                return val, None
        return value, "допустимые значения: " + ", ".join(f"{l} ({v})" for v, l in vs)
    if vtype in ("boolean", "bool", "checkbox"):
        b = bpmn.to_bool(value)
        return (b, None) if b is not None else (value, "нужно да или нет")
    if vtype in ("number", "integer", "int", "float"):
        try:
            f = float(str(value).replace(",", ".").replace(" ", ""))
            return (int(f) if f.is_integer() else f), None
        except ValueError:
            return value, "нужно число"
    if vtype in ("link", "url") and not str(value).strip().startswith(("http://", "https://")):
        return value, "нужна ссылка вида https://…"
    return value, None


def prop_label(prop: Dict[str, Any]) -> str:
    return str(prop.get("description") or prop.get("name") or prop.get("codeName"))


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def start_run(track: Track, user_id: str, user_name: str = "") -> Run:
    if track.status != TrackStatus.published:
        raise RuleError("запускать можно только опубликованный трек")
    run = Run(run_id=uuid.uuid4().hex, user_id=user_id, user_name=user_name,
              track_id=track.id, track_version=track.version, track_name=track.name,
              facts=dict(storage.load_profile(user_id)))
    first = bpmn.first_task(track.graph, run.facts)
    if first.kind != "task":
        raise RuleError("в схеме не найден первый шаг: " + (first.error or ", ".join(first.need)))
    enter_step(run, first.task)
    return run


def enter_step(run: Run, step_id: str) -> None:
    now = now_iso()
    if run.visits and run.visits[-1].left_at is None:
        run.visits[-1].left_at = now
    run.current_step = step_id
    run.visits.append(StepVisit(step_id=step_id, entered_at=now))


def step_issues(run: Run, step_id: str) -> list:
    return [i for i in run.jira_issues if i.step_id == step_id]


def needs_jira(track: Track, step_id: str) -> bool:
    node = track.graph.nodes.get(step_id)
    act = track.activity(step_id)
    if act and act.agent.jira is not None:
        return act.agent.jira.required
    return bool(node and node.task_type == "jira-send")


def missing_for_step(run: Run, track: Track, step_id: Optional[str] = None) -> List[str]:
    """What still blocks leaving the step, in words the agent can relay to the user."""
    sid = step_id or run.current_step
    act = track.activity(sid)
    if act is None:
        return []
    missing: List[str] = []
    for p in act.properties:
        if p.get("requiredToFillOut") and not p.get("isUnused") \
                and not is_filled(run.facts.get(p.get("codeName"))):
            missing.append(f"не заполнено «{prop_label(p)}» (код {p.get('codeName')})")
    done = run.confirmed.get(sid, {})
    for r in act.agent.requirements:
        if r.id not in done:
            missing.append(f"не подтверждено обязательное условие «{r.text}» (id {r.id})")
    if needs_jira(track, sid) and not step_issues(run, sid):
        missing.append("не создана задача Jira этого шага")
    nxt = bpmn.follow(track.graph, sid, run.facts)
    if nxt.kind == "need":
        props = track.props()
        for code in nxt.need:
            p = props.get(code, {"codeName": code})
            vs = variants(p)
            hint = f" (варианты: {', '.join(l for _, l in vs)})" if vs else \
                " (да/нет)" if str(p.get("valueType")).lower() in ("boolean", "bool") else ""
            item = f"для выбора следующего шага нужно значение «{prop_label(p)}» (код {code}){hint}"
            if item not in missing and not any(f"(код {code})" in m for m in missing):
                missing.append(item)
    elif nxt.kind == "error":
        missing.append(f"ошибка в схеме трека: {nxt.error}")
    return missing


def complete(run: Run, track: Track, summary: str = "") -> Optional[str]:
    """Finish the current step; the scheme decides where to go. Returns the new step id or None
    when the track is finished."""
    if run.status != RunStatus.active:
        raise RuleError(f"прохождение уже {run.status.value}")
    sid = run.current_step
    if sid not in track.graph.nodes:
        raise RuleError("текущий шаг не найден в схеме")
    missing = missing_for_step(run, track, sid)
    if missing:
        raise RuleError("нельзя продолжить, пока не выполнено: " + "; ".join(missing))
    nxt = bpmn.follow(track.graph, sid, run.facts)
    labels = [track.graph.flows[f].name for f in nxt.path if track.graph.flows[f].name]
    if nxt.kind == "end":
        finish(run, summary)
        return None
    enter_step(run, nxt.task)
    event(run, f"Шаг «{track.step_name(nxt.task)}»" + (f" ({' → '.join(labels)})" if labels else ""))
    return nxt.task


def go_back(run: Run, track: Track, step_id: str) -> str:
    if run.status != RunStatus.active:
        raise RuleError(f"прохождение уже {run.status.value}")
    if step_id == run.current_step or not any(v.step_id == step_id for v in run.visits):
        raise RuleError("вернуться можно только на уже пройденный шаг")
    enter_step(run, step_id)
    event(run, f"Возврат к шагу «{track.step_name(step_id)}»")
    return step_id


def finish(run: Run, summary: str = "") -> None:
    now = now_iso()
    if run.visits and run.visits[-1].left_at is None:
        run.visits[-1].left_at = now
    run.status = RunStatus.completed
    run.current_step = None
    run.finished_at = now
    run.summary = summary
    event(run, "Трек пройден" + (f": {summary}" if summary else ""))


def cancel(run: Run) -> None:
    if run.status != RunStatus.active:
        raise RuleError(f"прохождение уже {run.status.value}")
    if run.visits and run.visits[-1].left_at is None:
        run.visits[-1].left_at = now_iso()
    run.status = RunStatus.cancelled
    run.finished_at = now_iso()
    event(run, "Прохождение отменено")


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def step_links(track: Track, step_id: str) -> List[Link]:
    """Links from the step content plus extra links, with the audience set by the analyst."""
    act = track.activity(step_id)
    if act is None:
        return []
    out: List[Link] = []
    seen = set()
    for title, url in links.extract_links(act.content):
        if url in seen or not url.startswith(("http://", "https://")):
            continue
        seen.add(url)
        out.append(Link(title=title, url=url, audience=act.agent.link_audience.get(url, "both")))
    for l in act.agent.extra_links:
        if l.url not in seen:
            seen.add(l.url)
            out.append(l)
    return out


def progress(run: Run, track: Track) -> Dict[str, Any]:
    g = track.graph
    visited = {v.step_id for v in run.visits}
    steps = []
    for tid in bpmn.order(g):
        state = "current" if tid == run.current_step else "done" if tid in visited else "todo"
        steps.append({"id": tid, "title": track.step_name(tid), "group": g.group_of(tid),
                      "state": state})
    current = None
    sid = run.current_step
    act = track.activity(sid)
    if sid and act is not None:
        props = []
        for p in act.properties:
            if p.get("isUnused"):
                continue
            props.append({"code": p.get("codeName"), "name": p.get("name"),
                          "description": p.get("description"), "type": p.get("valueType"),
                          "variants": [{"value": v, "label": l} for v, l in variants(p)],
                          "required": bool(p.get("requiredToFillOut")),
                          "value": run.facts.get(p.get("codeName"))})
        current = {
            "id": sid, "title": track.step_name(sid), "group": g.group_of(sid),
            "content": act.content,
            "links": [l.model_dump() for l in step_links(track, sid) if l.audience != "agent"],
            "properties": props,
            "requirements": [{"id": r.id, "text": r.text,
                              "done": r.id in run.confirmed.get(sid, {})}
                             for r in act.agent.requirements],
            "jira": needs_jira(track, sid),
        }
    done = len([s for s in steps if s["state"] == "done"])
    if run.status == RunStatus.completed:
        percent = 100
    elif sid in g.nodes:
        percent = round(100 * done / (done + 1 + bpmn.remaining(g, sid)))
    else:
        percent = 0
    return {"steps": steps, "current": current, "percent": percent,
            "missing": missing_for_step(run, track) if sid and act is not None else []}
