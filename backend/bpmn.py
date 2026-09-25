"""BPMN 2.0 schemes in the format of the partner system (Camunda flavour).

- steps are `bpmn:serviceTask` with `camunda:property name="taskType"` = "task" | "jira-send";
- branching is done by exclusive gateways whose outgoing flows carry
  `${objProps.prop("<codeName>").value() == <true|false|"text"|number>}`;
- stages are `bpmn:group` elements; membership is geometric (task inside the group bounds).

Conditions are formal, so the next step is computed here, never guessed by the model.
"""

from __future__ import annotations

import json
import re
import secrets
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

NS = {
    "bpmn": "http://www.omg.org/spec/BPMN/20100524/MODEL",
    "bpmndi": "http://www.omg.org/spec/BPMN/20100524/DI",
    "dc": "http://www.omg.org/spec/DD/20100524/DC",
    "di": "http://www.omg.org/spec/DD/20100524/DI",
    "camunda": "http://camunda.org/schema/1.0/bpmn",
    "bioc": "http://bpmn.io/schema/bpmn/biocolor/1.0",
    "color": "http://www.omg.org/spec/BPMN/non-normative/color/1.0",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}
for _p, _u in NS.items():
    ET.register_namespace(_p, _u)

B = "{%s}" % NS["bpmn"]
TASK_TAGS = {"serviceTask", "task", "userTask", "manualTask", "scriptTask", "sendTask",
             "receiveTask", "businessRuleTask"}
GATEWAY_TAGS = {"exclusiveGateway", "inclusiveGateway", "parallelGateway", "eventBasedGateway",
                "complexGateway"}
TASK_TYPES = ("task", "jira-send")

COND_RE = re.compile(
    r'^\$\{\s*objProps\.prop\(\s*"([^"]+)"\s*\)\.value\(\)\s*(==|!=)\s*'
    r'(true|false|"[^"]*"|-?\d+(?:\.\d+)?)\s*\}$')


class BpmnError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------

@dataclass
class Condition:
    prop: str
    value: Any
    op: str = "=="

    def matches(self, actual: Any) -> bool:
        eq = _same(actual, self.value)
        return eq if self.op == "==" else not eq


def _same(actual: Any, expected: Any) -> bool:
    if isinstance(expected, bool):
        return to_bool(actual) is expected
    if isinstance(expected, (int, float)):
        try:
            return float(actual) == float(expected)
        except (TypeError, ValueError):
            return False
    return str(actual).strip().lower() == str(expected).strip().lower()


def to_bool(v: Any) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "да", "yes", "1", "y", "д", "ага", "конечно", "верно"):
        return True
    if s in ("false", "нет", "no", "0", "n", "н", "не"):
        return False
    return None


def parse_condition(text: Optional[str]) -> Optional[Condition]:
    if not text or not text.strip():
        return None
    m = COND_RE.match(text.strip())
    if not m:
        raise BpmnError(f"условие не в формате objProps.prop(\"код\").value() == значение: {text.strip()}")
    raw = m.group(3)
    if raw in ("true", "false"):
        value: Any = raw == "true"
    elif raw.startswith('"'):
        value = raw[1:-1]
    else:
        value = float(raw) if "." in raw else int(raw)
    return Condition(prop=m.group(1), value=value, op=m.group(2))


def format_condition(prop: str, value: Any, op: str = "==") -> str:
    if isinstance(value, bool):
        lit = "true" if value else "false"
    elif isinstance(value, (int, float)):
        lit = str(value)
    else:
        lit = json.dumps(str(value), ensure_ascii=False)
    return '${objProps.prop("%s").value() %s %s}' % (prop, op, lit)


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

@dataclass
class Node:
    id: str
    kind: str  # task | gateway | start | end | other
    name: str = ""
    task_type: Optional[str] = None
    incoming: List[str] = field(default_factory=list)
    outgoing: List[str] = field(default_factory=list)


@dataclass
class Flow:
    id: str
    source: str
    target: str
    name: str = ""
    condition_text: Optional[str] = None
    condition: Optional[Condition] = None
    condition_error: Optional[str] = None


@dataclass
class Group:
    id: str
    name: str
    members: List[str]


@dataclass
class Graph:
    process_id: str
    process_name: str
    nodes: Dict[str, Node]
    flows: Dict[str, Flow]
    groups: List[Group]
    bounds: Dict[str, Tuple[float, float, float, float]]

    @property
    def tasks(self) -> List[Node]:
        return [n for n in self.nodes.values() if n.kind == "task"]

    def starts(self) -> List[Node]:
        return [n for n in self.nodes.values() if n.kind == "start"]

    def group_of(self, node_id: str) -> Optional[str]:
        for g in self.groups:
            if node_id in g.members:
                return g.name
        return None


def clean_xml(text: str) -> str:
    """Exports of the partner system may start with a blank line before <?xml ...?>."""
    return text.lstrip("﻿ \t\r\n")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse(xml_text: str) -> Graph:
    try:
        root = ET.fromstring(clean_xml(xml_text).encode("utf-8"))
    except ET.ParseError as e:
        line = e.position[0] if hasattr(e, "position") else "?"
        raise BpmnError(f"схема BPMN повреждена (строка {line}): {e}") from e
    proc = root.find("bpmn:process", NS)
    if proc is None:
        raise BpmnError("в схеме нет bpmn:process")
    nodes: Dict[str, Node] = {}
    flows: Dict[str, Flow] = {}
    for el in proc:
        tag = _local(el.tag)
        nid = el.get("id", "")
        if tag == "sequenceFlow":
            ce = el.find("bpmn:conditionExpression", NS)
            text = ce.text.strip() if ce is not None and ce.text else None
            f = Flow(id=nid, source=el.get("sourceRef", ""), target=el.get("targetRef", ""),
                     name=(el.get("name") or "").strip(), condition_text=text)
            try:
                f.condition = parse_condition(text)
            except BpmnError as e:
                f.condition_error = str(e)
            flows[nid] = f
            continue
        if tag in TASK_TAGS:
            kind = "task"
        elif tag in GATEWAY_TAGS:
            kind = "gateway"
        elif tag == "startEvent":
            kind = "start"
        elif tag == "endEvent":
            kind = "end"
        elif tag in ("group", "category", "textAnnotation", "association", "extensionElements"):
            continue
        else:
            kind = "other"
        node = Node(id=nid, kind=kind, name=(el.get("name") or "").strip())
        if kind == "task":
            prop = el.find("bpmn:extensionElements/camunda:property[@name='taskType']", NS)
            node.task_type = prop.get("value") if prop is not None else None
        nodes[nid] = node
    for f in flows.values():
        if f.source in nodes:
            nodes[f.source].outgoing.append(f.id)
        if f.target in nodes:
            nodes[f.target].incoming.append(f.id)

    bounds: Dict[str, Tuple[float, float, float, float]] = {}
    for shape in root.iter("{%s}BPMNShape" % NS["bpmndi"]):
        b = shape.find("dc:Bounds", NS)
        if b is not None:
            bounds[shape.get("bpmnElement", "")] = tuple(
                float(b.get(k, 0)) for k in ("x", "y", "width", "height"))  # type: ignore

    categories = {cv.get("id"): (cv.get("value") or "").strip()
                  for cv in root.iter(B + "categoryValue")}
    groups: List[Group] = []
    for g in proc.findall("bpmn:group", NS):
        gb = bounds.get(g.get("id", ""))
        members = []
        if gb:
            gx, gy, gw, gh = gb
            for n in nodes.values():
                nb = bounds.get(n.id)
                if n.kind == "task" and nb:
                    cx, cy = nb[0] + nb[2] / 2, nb[1] + nb[3] / 2
                    if gx <= cx <= gx + gw and gy <= cy <= gy + gh:
                        members.append(n.id)
        groups.append(Group(id=g.get("id", ""), name=categories.get(g.get("categoryValueRef"), ""),
                            members=members))
    return Graph(process_id=proc.get("id", ""), process_name=proc.get("name", ""),
                 nodes=nodes, flows=flows, groups=groups, bounds=bounds)


# ---------------------------------------------------------------------------
# Traversal
# ---------------------------------------------------------------------------

@dataclass
class Next:
    kind: str  # task | end | need | error
    task: Optional[str] = None
    need: List[str] = field(default_factory=list)
    error: str = ""
    path: List[str] = field(default_factory=list)  # flows taken, for the event log


def _is_filled(v: Any) -> bool:
    return v not in (None, "", [], {})


def follow(g: Graph, from_node: str, facts: Dict[str, Any]) -> Next:
    """From a node, take its outgoing flow(s) through gateways to the next task or end event."""
    cur = g.nodes.get(from_node)
    if cur is None:
        return Next("error", error=f"узел {from_node} не найден в схеме")
    path: List[str] = []
    seen = set()
    while True:
        if cur.id in seen:
            return Next("error", error="цикл из шлюзов без задач", path=path)
        seen.add(cur.id)
        outs = [g.flows[f] for f in cur.outgoing if f in g.flows]
        if not outs:
            if cur.kind == "end":
                return Next("end", path=path)
            return Next("error", error=f"у «{cur.name or cur.id}» нет исходящих переходов", path=path)
        if len(outs) == 1 and outs[0].condition is None:
            flow = outs[0]
        else:
            need, match, default = [], [], None
            for f in outs:
                if f.condition_error:
                    return Next("error", error=f.condition_error, path=path)
                if f.condition is None:
                    default = default or f
                elif not _is_filled(facts.get(f.condition.prop)):
                    if f.condition.prop not in need:
                        need.append(f.condition.prop)
                elif f.condition.matches(facts.get(f.condition.prop)):
                    match.append(f)
            if match:
                flow = match[0]
            elif need:
                return Next("need", need=need, path=path)
            elif default:
                flow = default
            else:
                vals = ", ".join(f"{f.condition.prop} = {facts.get(f.condition.prop)!r}"
                                 for f in outs if f.condition)
                return Next("error", error=f"ни одна ветка не подходит ({vals})", path=path)
        path.append(flow.id)
        cur = g.nodes.get(flow.target)
        if cur is None:
            return Next("error", error=f"переход {flow.id} ведёт в несуществующий узел", path=path)
        if cur.kind == "task":
            return Next("task", task=cur.id, path=path)
        if cur.kind == "end":
            return Next("end", path=path)


def first_task(g: Graph, facts: Dict[str, Any]) -> Next:
    starts = g.starts()
    if not starts:
        return Next("error", error="в схеме нет стартового события")
    return follow(g, starts[0].id, facts)


def decisions_after(g: Graph, task_id: str) -> List[Dict[str, Any]]:
    """Gateway decisions reached from a task before any other task: what the agent must learn."""
    out: List[Dict[str, Any]] = []
    stack, seen = [task_id], set()
    while stack:
        nid = stack.pop()
        for fid in g.nodes[nid].outgoing if nid in g.nodes else []:
            f = g.flows.get(fid)
            if not f:
                continue
            tgt = g.nodes.get(f.target)
            if f.condition:
                out.append({"prop": f.condition.prop, "value": f.condition.value,
                            "op": f.condition.op, "label": f.name,
                            "to": (tgt.name or tgt.id) if tgt else f.target})
            if tgt and tgt.kind == "gateway" and tgt.id not in seen:
                seen.add(tgt.id)
                stack.append(tgt.id)
    return out


def order(g: Graph) -> List[str]:
    """Tasks in reading order: BFS from the start, conditional branches in document order."""
    out: List[str] = []
    seen = set()
    queue = [s.id for s in g.starts()]
    while queue:
        nid = queue.pop(0)
        if nid in seen or nid not in g.nodes:
            continue
        seen.add(nid)
        if g.nodes[nid].kind == "task":
            out.append(nid)
        queue.extend(g.flows[f].target for f in g.nodes[nid].outgoing if f in g.flows)
    out.extend(n.id for n in g.tasks if n.id not in seen)
    return out


def remaining(g: Graph, task_id: str) -> int:
    """Fewest tasks from here (exclusive) to an end event, ignoring conditions."""
    dist = {task_id: 0}
    queue = [task_id]
    best: Optional[int] = None
    while queue:
        nid = queue.pop(0)
        for fid in g.nodes[nid].outgoing:
            t = g.flows[fid].target if fid in g.flows else None
            if t is None or t not in g.nodes or t in dist:
                continue
            d = dist[nid] + (1 if g.nodes[t].kind == "task" else 0)
            if g.nodes[t].kind == "end":
                best = d if best is None else min(best, d)
                continue
            dist[t] = d
            queue.append(t)
    return best if best is not None else 0


def lint(g: Graph, props_by_code: Dict[str, Dict[str, Any]],
         activities: Iterable[str]) -> Dict[str, List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    acts = set(activities)
    if not g.starts():
        errors.append("нет стартового события")
    if not any(n.kind == "end" for n in g.nodes.values()):
        errors.append("нет завершающего события")
    for f in g.flows.values():
        for ref in (f.source, f.target):
            if ref not in g.nodes:
                errors.append(f"переход «{f.name or f.id}» ссылается на несуществующий элемент {ref}")
    if g.starts() and first_task(g, {}).kind not in ("task", "need"):
        errors.append("от стартового события не достижим ни один шаг")
    for n in g.nodes.values():
        label = f"«{n.name or n.id}»"
        if n.kind == "task":
            if n.id not in acts:
                errors.append(f"у задачи {label} нет описания активности (Activity)")
            if len(n.outgoing) != 1:
                errors.append(f"у задачи {label} должен быть ровно один исходящий переход, "
                              f"сейчас {len(n.outgoing)} (ветвление делается шлюзом)")
            if not n.incoming:
                warnings.append(f"в задачу {label} не ведёт ни один переход")
            if n.task_type not in TASK_TYPES:
                warnings.append(f"у задачи {label} не задан тип (task / jira-send)")
        if n.kind == "gateway" and len(n.outgoing) > 1:
            outs = [g.flows[f] for f in n.outgoing if f in g.flows]
            for f in outs:
                if f.condition_error:
                    errors.append(f"переход «{f.name or f.id}»: {f.condition_error}")
                elif f.condition is None:
                    warnings.append(f"у ветки «{f.name or f.id}» из шлюза нет условия — "
                                    f"она будет выбрана, если не подойдёт ни одна другая")
                elif f.condition.prop not in props_by_code:
                    errors.append(f"условие ветки «{f.name or f.id}» ссылается на свойство "
                                  f"«{f.condition.prop}», которого нет ни в одной активности")
        if n.kind == "other":
            warnings.append(f"элемент {label} не поддерживается движком и будет пропущен")
    reach = set(order(g)) if g.starts() else set()
    for n in g.tasks:
        if n.id not in reach:
            warnings.append(f"задача «{n.name or n.id}» недостижима от старта")
    return {"errors": errors, "warnings": warnings}


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def normalize(xml_text: str, task_types: Dict[str, str]) -> str:
    """Bring task elements to the partner format: bpmn:serviceTask, camunda:type="external",
    camunda:topic="xray", extension property taskType. Returns the input unchanged when it
    already conforms, so files produced by the partner system stay byte-identical."""
    text = clean_xml(xml_text)
    root = ET.fromstring(text.encode("utf-8"))
    proc = root.find("bpmn:process", NS)
    if proc is None:
        raise BpmnError("в схеме нет bpmn:process")
    changed = False
    for el in list(proc):
        tag = _local(el.tag)
        if tag not in TASK_TAGS:
            continue
        if tag != "serviceTask":
            el.tag = B + "serviceTask"
            changed = True
        for attr, val in ((f"{{{NS['camunda']}}}type", "external"),
                          (f"{{{NS['camunda']}}}topic", "xray")):
            if el.get(attr) is None:
                el.set(attr, val)
                changed = True
        want = task_types.get(el.get("id", ""), None)
        ext = el.find("bpmn:extensionElements", NS)
        if ext is None:
            ext = ET.Element(B + "extensionElements")
            el.insert(0, ext)
            changed = True
        prop = ext.find("camunda:property[@name='taskType']", NS)
        if prop is None:
            prop = ET.SubElement(ext, f"{{{NS['camunda']}}}property", {"name": "taskType"})
            prop.set("value", want or "task")
            changed = True
        elif want and prop.get("value") != want:
            prop.set("value", want)
            changed = True
    if not changed:
        return xml_text
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def build(process_id: str, name: str, tasks: List[Dict[str, Any]],
          flows: List[Dict[str, Any]]) -> str:
    """Generate a scheme with layout. tasks: [{id, name, type}], flows: [{from, to, name?,
    prop?, value?}]; "start"/"end" are the start and end events; `gw:<id>` creates a gateway.
    Layout is left-to-right by longest path, like schemes drawn in the partner system."""
    ids = ["start"] + [t["id"] for t in tasks]
    gws = sorted({f[k] for f in flows for k in ("from", "to") if str(f[k]).startswith("gw:")})
    ids += gws + ["end"]
    out: Dict[str, List[str]] = {i: [] for i in ids}
    for f in flows:
        out[f["from"]].append(f["to"])
    layer = {"start": 0}
    changed = True
    guard = 0
    while changed and guard < len(ids) * 2:  # longest path; back edges are ignored by the guard
        changed, guard = False, guard + 1
        for f in flows:
            if f["from"] in layer and layer.get(f["to"], -1) < layer[f["from"]] + 1 \
                    and layer[f["from"]] + 1 < len(ids):
                layer[f["to"]] = layer[f["from"]] + 1
                changed = True
    for i in ids:
        layer.setdefault(i, 1)
    layer["end"] = max(layer.values()) + (0 if out["end"] else 1)
    rows: Dict[int, List[str]] = {}
    for i in ids:
        rows.setdefault(layer[i], []).append(i)
    size = {"task": (100, 80), "gw": (50, 50), "event": (36, 36)}
    pos: Dict[str, Tuple[float, float, float, float]] = {}
    for l, members in rows.items():
        for k, i in enumerate(members):
            kind = "gw" if i.startswith("gw:") else "event" if i in ("start", "end") else "task"
            w, h = size[kind]
            cx, cy = 150 + l * 170, 120 + k * 150
            pos[i] = (cx - w / 2, cy - h / 2, w, h)

    gw_ids = {gw: new_id("Gateway") for gw in gws}
    end_id = new_id("Event")

    def xid(i: str) -> str:
        return {"start": "StartEvent_1", "end": end_id}.get(i) or gw_ids.get(i) or i

    E = ET.Element
    root = E(f"{B}definitions", {"id": "Definitions_1",
                                 "targetNamespace": "http://bpmn.io/schema/bpmn"})
    proc = ET.SubElement(root, f"{B}process", {"id": process_id, "name": process_id,
                                               "isExecutable": "true"})
    elems: Dict[str, ET.Element] = {}
    elems["start"] = ET.SubElement(proc, f"{B}startEvent", {"id": xid("start")})
    for t in tasks:
        el = ET.SubElement(proc, f"{B}serviceTask", {
            "id": t["id"], "name": t["name"],
            f"{{{NS['camunda']}}}type": "external", f"{{{NS['camunda']}}}topic": "xray"})
        ext = ET.SubElement(el, f"{B}extensionElements")
        ET.SubElement(ext, f"{{{NS['camunda']}}}property",
                      {"name": "taskType", "value": t.get("type") or "task"})
        elems[t["id"]] = el
    for gw in gws:
        elems[gw] = ET.SubElement(proc, f"{B}exclusiveGateway", {"id": xid(gw)})
    elems["end"] = ET.SubElement(proc, f"{B}endEvent", {"id": xid("end")})
    flow_ids = []
    for f in flows:
        fid = new_id("Flow")
        flow_ids.append(fid)
        ET.SubElement(elems[f["from"]], f"{B}outgoing").text = fid
        ET.SubElement(elems[f["to"]], f"{B}incoming").text = fid
        attrs = {"id": fid, "sourceRef": xid(f["from"]), "targetRef": xid(f["to"])}
        if f.get("name"):
            attrs["name"] = f["name"]
        sf = ET.SubElement(proc, f"{B}sequenceFlow", attrs)
        if "prop" in f:
            ce = ET.SubElement(sf, f"{B}conditionExpression",
                               {f"{{{NS['xsi']}}}type": "bpmn:tFormalExpression"})
            ce.text = format_condition(f["prop"], f["value"])
    DI = "{%s}" % NS["bpmndi"]
    diagram = ET.SubElement(root, f"{DI}BPMNDiagram", {"id": "BPMNDiagram_1"})
    plane = ET.SubElement(diagram, f"{DI}BPMNPlane", {"id": "BPMNPlane_1",
                                                      "bpmnElement": process_id})
    for i in ids:
        x, y, w, h = pos[i]
        attrs = {"id": f"{xid(i)}_di", "bpmnElement": xid(i)}
        if i.startswith("gw:"):
            attrs["isMarkerVisible"] = "true"
        shape = ET.SubElement(plane, f"{DI}BPMNShape", attrs)
        ET.SubElement(shape, f"{{{NS['dc']}}}Bounds",
                      {"x": _n(x), "y": _n(y), "width": _n(w), "height": _n(h)})
    lane = 0
    for fid, f in zip(flow_ids, flows):
        sx, sy, sw, sh = pos[f["from"]]
        tx, ty, tw, th = pos[f["to"]]
        edge = ET.SubElement(plane, f"{DI}BPMNEdge", {"id": f"{fid}_di", "bpmnElement": fid})
        p1 = (sx + sw, sy + sh / 2)
        p2 = (tx, ty + th / 2)
        between = [pos[i] for i in ids if layer[i] > layer[f["from"]] and layer[i] < layer[f["to"]]]
        if tx <= sx or between:  # back edge or skipping layers: go around below, own lane per edge
            lane += 1
            low = max([sy + sh, ty + th] + [b[1] + b[3] for b in between]) + 20 + 22 * lane
            pts = [(sx + sw / 2, sy + sh), (sx + sw / 2, low), (tx + tw / 2, low), (tx + tw / 2, ty + th)]
        elif abs(p1[1] - p2[1]) < 1:
            pts = [p1, p2]
        else:
            mx = (p1[0] + p2[0]) / 2
            pts = [p1, (mx, p1[1]), (mx, p2[1]), p2]
        for px, py in pts:
            ET.SubElement(edge, f"{{{NS['di']}}}waypoint", {"x": _n(px), "y": _n(py)})
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def new_id(prefix: str) -> str:
    """bpmn-js style id: Activity_1huk1tj, Flow_0wwd0xa, Gateway_0w17ot6."""
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    return prefix + "_" + str(secrets.randbelow(2)) + "".join(secrets.choice(alphabet) for _ in range(6))


def _n(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else str(round(v, 1))
