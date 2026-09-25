"""Keeping a track consistent: scheme.bpmn is the source of truth for which steps exist,
Activity_* folders describe them. Also zip import/export in the partner system's layout."""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional

from backend import bpmn
from backend.models import (SLUG_RE, Activity, AgentExtras, Track, TrackStatus, new_activity_metadata,
                            new_property)

TRACK_TYPES = bpmn.TASK_TYPES
VALUE_TYPES = ("string", "text", "boolean", "number", "date", "link", "select", "user")


def task_type(act: Optional[Activity], node: Optional[bpmn.Node]) -> str:
    """BPMN taskType wins; metadata.type is kept equal to it."""
    if node and node.task_type in TRACK_TYPES:
        return node.task_type
    t = (act.metadata.get("type") if act else None) or "task"
    return t if t in TRACK_TYPES else "task"


def sync(track: Track, task_types: Optional[Dict[str, str]] = None) -> Track:
    """Align activities with the scheme and bring the scheme to the partner format.
    `task_types` (task id -> task|jira-send) comes from the editor."""
    explicit = dict(task_types or {})
    g = track.graph
    task_types = {n.id: explicit.get(n.id) or task_type(track.activities.get(n.id), n)
                  for n in g.tasks}
    xml = bpmn.normalize(track.bpmn, task_types)
    g = bpmn.parse(xml)
    ids = [n.id for n in g.tasks]
    order = [t for t in track.tasks if t in ids] + [t for t in ids if t not in track.tasks]
    next_order = max([int(a.metadata.get("displayOrder") or 0) for a in track.activities.values()]
                     + [0])
    activities: Dict[str, Activity] = {}
    for tid in order:
        node = g.nodes[tid]
        act = track.activities.get(tid)
        if act is None:
            next_order += 1
            act = Activity(metadata=new_activity_metadata(tid, node.name, next_order))
        md = act.metadata  # partner files: fill what is missing, never rewrite what is there
        for k, v in new_activity_metadata(tid, node.name, next_order + 1).items():
            md.setdefault(k, v)  # appended at the end: existing key order stays byte-identical
        md.setdefault("codeName", tid)
        if not md.get("name"):
            md["name"] = node.name
        if tid in explicit or not md.get("type"):
            md["type"] = task_types[tid]
        props = []
        for p in act.properties:
            p = {**new_property(track.track_uuid), **p}
            p["trackId"] = p.get("trackId") or track.track_uuid
            props.append(p)
        act.properties = props
        if [p["id"] for p in props] != md.get("properties"):
            md["properties"] = [p["id"] for p in props]
        activities[tid] = act
    return track.model_copy(update={"bpmn": xml, "tasks": order, "activities": activities})


def lint(track: Track) -> Dict[str, List[str]]:
    g = track.graph
    result = bpmn.lint(g, track.props(), track.activities.keys())
    errors, warnings = result["errors"], result["warnings"]
    seen: Dict[str, str] = {}
    for tid, act in track.activities.items():
        label = f"«{track.step_name(tid)}»"
        if tid not in g.nodes:
            warnings.append(f"активность {tid} не используется в схеме и будет удалена при сохранении")
        if not act.content.strip() and not act.agent.agent_instructions.strip():
            warnings.append(f"у шага {label} нет описания")
        for p in act.properties:
            code = p.get("codeName") or ""
            if not code:
                errors.append(f"у свойства «{p.get('name') or '?'}» шага {label} нет кода (codeName)")
            elif code in seen and seen[code] != tid:
                warnings.append(f"свойство «{code}» объявлено в нескольких шагах, "
                                f"значение будет общим")
            seen.setdefault(code, tid)
        if act.agent.jira and act.agent.jira.placeholders():
            for k in act.agent.jira.placeholders():
                if k not in seen and k not in track.props():
                    warnings.append(f"шаг {label}: {{{k}}} в шаблоне Jira не собирается ни на одном "
                                    f"шаге, агент спросит у пользователя")
    return {"errors": errors, "warnings": warnings}


# ---------------------------------------------------------------------------
# New tracks and generated schemes
# ---------------------------------------------------------------------------

def from_spec(meta: Dict[str, Any], steps: List[Dict[str, Any]]) -> Track:
    """Build a track from a simple spec (used for new tracks, drafts and demos).
    steps: [{id?, name, type?, content?, agent_instructions?, properties: [{codeName, name,
    valueType?, valueVariants?, required?, description?}], requirements?, jira?,
    next: [{to: step index or id or "end", prop?, value?, name?}]}] — no `next` means the
    following step (or end for the last one)."""
    ids: List[str] = []
    for st in steps:
        sid = st.get("id") or ""
        ids.append(sid if re.fullmatch(r"Activity_[A-Za-z0-9_]+", sid) else bpmn.new_id("Activity"))
    by_ref = {st.get("id"): ids[i] for i, st in enumerate(steps) if st.get("id")}

    def ref(to: Any) -> str:
        if to in ("end", "__finish__", None):
            return "end"
        if isinstance(to, int):
            return ids[to]
        return by_ref.get(to, to if to in ids else "end")

    flows: List[Dict[str, Any]] = [{"from": "start", "to": ids[0] if ids else "end"}]
    gw_n = 0
    for i, st in enumerate(steps):
        nxt = st.get("next")
        if not nxt:
            flows.append({"from": ids[i], "to": ids[i + 1] if i + 1 < len(ids) else "end"})
            continue
        conditional = [b for b in nxt if "prop" in b]
        if not conditional:
            flows.append({"from": ids[i], "to": ref(nxt[0].get("to"))})
            continue
        gw_n += 1
        gw = f"gw:{gw_n:03d}"
        flows.append({"from": ids[i], "to": gw})
        for b in nxt:
            f = {"from": gw, "to": ref(b.get("to")), "name": b.get("name", "")}
            if "prop" in b:
                f.update(prop=b["prop"], value=b["value"])
            flows.append(f)
    tasks = [{"id": ids[i], "name": st["name"], "type": st.get("type", "task")}
             for i, st in enumerate(steps)]
    xml = bpmn.build(meta["id"].replace("-", "_"), meta["name"], tasks, flows)
    track = Track.model_validate({**meta, "bpmn": xml, "tasks": ids, "activities": {}})
    acts: Dict[str, Activity] = {}
    for i, st in enumerate(steps):
        md = new_activity_metadata(ids[i], st["name"], i + 1, st.get("type", "task"))
        md["content"] = st.get("content", "")
        props = []
        for p in st.get("properties", []):
            if not p.get("codeName"):
                continue
            prop = new_property(track.track_uuid, p["codeName"], p.get("name", p["codeName"]))
            prop.update(description=p.get("description", ""),
                        valueType=p.get("valueType", "string"),
                        valueVariants=p.get("valueVariants", []),
                        requiredToFillOut=p.get("required", True))
            props.append(prop)
        md["properties"] = [p["id"] for p in props]
        extras = AgentExtras.model_validate({k: st[k] for k in ("agent_instructions",
                                                                "requirements", "jira",
                                                                "extra_links") if k in st})
        acts[ids[i]] = Activity(metadata=md, properties=props, agent=extras)
    return sync(track.model_copy(update={"activities": acts}))


def new_track(track_id: str, name: str, description: str = "") -> Track:
    return from_spec({"id": track_id, "name": name, "description": description},
                     [{"name": "Первый шаг"}])


# ---------------------------------------------------------------------------
# Zip import / export
# ---------------------------------------------------------------------------

def export_zip(track: Track, pure: bool = False) -> bytes:
    """The track folder as a zip. `pure` leaves out our sidecars (track.json, agent.json)."""
    from backend import storage

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("scheme.bpmn", track.bpmn)
        z.writestr("tasks.json", storage._compact(track.tasks))
        if not pure:
            z.writestr("track.json", json.dumps(track.meta().model_dump(mode="json"),
                                                ensure_ascii=False, indent=2))
        for tid, act in track.activities.items():
            z.writestr(f"{tid}/metadata.json", storage._compact(act.metadata))
            z.writestr(f"{tid}/properties.json", storage._compact(act.properties))
            z.writestr(f"{tid}/attachments.json", storage._compact(act.attachments))
            agent = act.agent.model_dump(mode="json", exclude_defaults=True)
            if agent and not pure:
                z.writestr(f"{tid}/agent.json", json.dumps(agent, ensure_ascii=False, indent=2))
    return buf.getvalue()


def import_zip(data: bytes, track_id: Optional[str] = None, name: Optional[str] = None) -> Track:
    """Read a zip with scheme.bpmn + tasks.json + Activity_*/ (possibly inside one top folder)."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ValueError("файл не является zip-архивом") from e
    files: Dict[str, bytes] = {}
    for info in z.infolist():
        if info.is_dir() or info.file_size > 20_000_000:
            continue
        p = PurePosixPath(info.filename)
        if ".." in p.parts or p.is_absolute() or p.name.startswith("._") or "__MACOSX" in p.parts:
            continue
        files[str(p)] = z.read(info)
    scheme = [f for f in files if PurePosixPath(f).name == "scheme.bpmn"]
    if not scheme:
        raise ValueError("в архиве нет scheme.bpmn")
    root = PurePosixPath(scheme[0]).parent

    def rel(f: str) -> Optional[PurePosixPath]:
        p = PurePosixPath(f)
        try:
            return p.relative_to(root)
        except ValueError:
            return None

    def load(raw: bytes) -> Any:
        return json.loads(raw.decode("utf-8-sig"))

    xml = files[scheme[0]].decode("utf-8-sig")
    meta: Dict[str, Any] = {}
    tasks: List[str] = []
    acts: Dict[str, Dict[str, Any]] = {}
    for f, raw in files.items():
        r = rel(f)
        if r is None:
            continue
        if r.parts == ("track.json",):
            meta = load(raw)
        elif r.parts == ("tasks.json",):
            tasks = load(raw)
        elif len(r.parts) == 2 and r.parts[1] in ("metadata.json", "properties.json",
                                                   "attachments.json", "agent.json"):
            key = r.parts[1].removesuffix(".json")
            acts.setdefault(r.parts[0], {})[key] = load(raw)
    acts = {k: v for k, v in acts.items() if "metadata" in v}
    g = bpmn.parse(xml)  # readable error for a damaged scheme
    track_uuid = next((p.get("trackId") for a in acts.values() for p in a.get("properties", [])
                       if isinstance(p, dict) and p.get("trackId")), None)
    tid = track_id or meta.get("id") or _slug_from(g.process_id or root.name or "track")
    meta.update(id=tid, name=name or meta.get("name") or g.process_name or tid,
                status=meta.get("status") or TrackStatus.draft.value)
    if track_uuid and not meta.get("track_uuid"):
        meta["track_uuid"] = track_uuid
    meta.pop("version", None)
    track = Track.model_validate({**meta, "bpmn": xml, "tasks": tasks,
                                  "activities": {k: Activity.model_validate(v)
                                                 for k, v in acts.items()}})
    return track


def _slug_from(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", text).strip("-")[:60]
    return s if s and SLUG_RE.match(s) else "track"
