"""The track agent: builds context for the current step, runs the tool-calling loop and applies
tool effects to the run. Every state change goes through `engine`, which enforces the rules;
the next step is chosen by the BPMN scheme, not by the model."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend import bpmn, engine, jira, links, llm, storage, tracks
from backend.models import (ChatMessage, JiraIssueRef, JiraTemplate, Run, RunStatus, Settings,
                            Track)

logger = logging.getLogger(__name__)

MAX_ROUNDS = 8
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
START_PROMPT = ("(Служебное: пользователь только что начал трек. Поприветствуй его одной фразой "
                "и начни первый шаг.)")
RESUME_PROMPT = ("(Служебное: пользователь вернулся к треку после перерыва. Кратко напомни, "
                 "на каком он шаге и что осталось сделать.)")
NUDGE_PROMPT = ("(Служебное: всё необходимое для текущего шага выполнено, но шаг не завершён. "
                "Вызови complete_step и начни следующий шаг. Пользователю не повторяй уже сказанное.)")

BASE_PROMPT = """Ты — агент-проводник. Ты ведёшь пользователя по треку шаг за шагом, пока трек не будет пройден.

Как работать:
1. Работай только с текущим шагом, шаги не пропускай. «Описание шага» — это инструкция для пользователя: перескажи её своими словами, коротко и по делу, с нужными ссылками. «Инструкции для агента» — для тебя. Материалы по ссылкам уже прочитаны и приведены ниже; если материал обрезан, дочитай его через read_link.
2. «Свойства шага» — данные, которые нужно получить от пользователя. Спрашивай их простыми словами (не показывай коды), для вариантов перечисли варианты. Любые данные, которые сообщил пользователь (даже для будущих шагов), сразу сохраняй через save_info, ключ — код свойства. Если факт полезен и в будущих треках (имя, команда, роль), ставь remember=true.
3. Никогда не переспрашивай то, что уже есть в «Собранных данных». Если пользователь исправил сказанное раньше, сразу перезапиши значение через save_info.
4. Обязательное условие подтверждай через confirm_requirement только тогда, когда пользователь явно подтвердил, что оно выполнено. Если в описании шага сказано, что переход на следующий этап означает подтверждение чего-либо, перед complete_step получи от пользователя явное «да».
5. Задача Jira: посмотри, каких данных не хватает, недостающее запроси одним сообщением, затем вызови create_jira_issue и сообщи номер задачи и ссылку. Если создать задачу не получается (Jira недоступна), дай пользователю готовые поля для ручного создания, попроси прислать ключ задачи и привяжи его через link_jira_issue. Статус задачи по просьбе пользователя проверяй через check_jira_status.
6. Когда шаг выполнен, вызови complete_step. Следующий шаг выберет схема трека по значениям свойств — поэтому заранее узнай значения, нужные для развилки (они перечислены в «Что дальше»). Если сервер отказал, объясни пользователю, чего не хватает. После перехода сразу начинай новый шаг в том же ответе.
7. Если пользователь хочет вернуться к пройденному шагу, вызови go_back.
8. Ответы пользователя принимай в свободной форме. Уточняй, только если ответ неоднозначен или сервер его не принял. Относительные даты («в пятницу») переводи в конкретную дату сам по текущей дате.
9. Отвечай на русском. Можно использовать Markdown: списки, **жирный**, ссылки."""

TOOLS: List[Dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "save_info",
        "description": "Сохранить данные пользователя. Ключ — код свойства (codeName).",
        "parameters": {"type": "object", "properties": {
            "data": {"type": "object", "description": "код свойства → значение"},
            "remember": {"type": "boolean",
                         "description": "запомнить и для будущих треков пользователя"}},
            "required": ["data"]}}},
    {"type": "function", "function": {
        "name": "confirm_requirement",
        "description": "Отметить обязательное условие текущего шага выполненным "
                       "(только после явного подтверждения пользователя).",
        "parameters": {"type": "object", "properties": {
            "requirement_id": {"type": "string"},
            "evidence": {"type": "string", "description": "что сказал пользователь"}},
            "required": ["requirement_id", "evidence"]}}},
    {"type": "function", "function": {
        "name": "read_link",
        "description": "Прочитать документ по ссылке из трека (продолжение с offset).",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}, "offset": {"type": "integer"}},
            "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "create_jira_issue",
        "description": "Создать задачу Jira для текущего шага.",
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string", "description": "заголовок, если его нет в шаблоне"},
            "description": {"type": "string", "description": "описание / дополнение к шаблону"}}}}},
    {"type": "function", "function": {
        "name": "link_jira_issue",
        "description": "Привязать к текущему шагу задачу Jira, которую пользователь создал сам.",
        "parameters": {"type": "object", "properties": {
            "issue_key": {"type": "string", "description": "ключ задачи, например REL-123"},
            "summary": {"type": "string"}},
            "required": ["issue_key"]}}},
    {"type": "function", "function": {
        "name": "check_jira_status",
        "description": "Узнать статус задачи Jira.",
        "parameters": {"type": "object", "properties": {"issue_key": {"type": "string"}},
                       "required": ["issue_key"]}}},
    {"type": "function", "function": {
        "name": "complete_step",
        "description": "Завершить текущий шаг. Сервер проверит обязательные данные, условия и "
                       "Jira и по схеме трека перейдёт на следующий шаг (или завершит трек).",
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string", "description": "коротко, что сделано"}}}}},
    {"type": "function", "function": {
        "name": "go_back",
        "description": "Вернуться на уже пройденный шаг.",
        "parameters": {"type": "object", "properties": {"step_id": {"type": "string"}},
                       "required": ["step_id"]}}},
]


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "да" if value else "нет"
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def render_template(text: str, facts: Dict[str, Any]) -> str:
    return re.sub(r"\{([a-zA-Z0-9_-]+)\}",
                  lambda m: _fmt(facts[m.group(1)]) if m.group(1) in facts else m.group(0), text)


def _link_block(track: Track, step_id: str, settings: Settings) -> str:
    items = engine.step_links(track, step_id)
    if not items:
        return ""
    who = {"agent": "для агента", "user": "для пользователя", "both": "для агента и пользователя"}
    budget = settings.links.max_chars * 3
    parts = ["### Материалы шага"]
    for l in items:
        head = f"- [{l.title or l.url}]({l.url}) — {who[l.audience]}"
        try:
            text = links.fetch_text(l.url, settings.links)
            cut = min(settings.links.max_chars, max(budget, 1500))
            budget -= cut
            more = f"\n  …(обрезано, всего {len(text)} символов — read_link с offset={cut})" \
                if len(text) > cut else ""
            parts.append(f"{head}\n<<<\n{text[:cut]}\n>>>{more}")
        except Exception as e:
            parts.append(f"{head}\n  (не удалось прочитать: {e}; опирайся на название ссылки)")
    return "\n".join(parts)


def _prop_line(p: Dict[str, Any], run: Run) -> str:
    code = p.get("codeName")
    val = run.facts.get(code)
    state = f"= {_fmt(val)}" if engine.is_filled(val) else "НЕ ЗАПОЛНЕНО"
    vs = engine.variants(p)
    kind = str(p.get("valueType") or "string")
    extra = f", варианты: {', '.join(f'{l} ({v})' for v, l in vs)}" if vs else f", тип {kind}"
    req = ", обязательно" if p.get("requiredToFillOut") else ""
    return f"- {code} — {engine.prop_label(p)}{extra}{req}: {state}"


def _jira_template(track: Track, step_id: str, settings: Settings):
    """Our template from agent.json, else hints from the partner's jiraSendTask."""
    act = track.activity(step_id)
    if act and act.agent.jira:
        return act.agent.jira
    raw = (act.metadata.get("jiraSendTask") if act else None) or {}
    t = JiraTemplate()
    if isinstance(raw, dict):
        t.project = str(raw.get("project") or raw.get("projectKey") or "")
        t.issue_type = str(raw.get("issueType") or raw.get("issue_type") or "Task")
        t.summary = str(raw.get("summary") or raw.get("title") or "")
        t.description = str(raw.get("description") or "")
    return t


def step_brief(run: Run, track: Track, step_id: str, settings: Settings) -> str:
    g = track.graph
    act = track.activity(step_id)
    group = g.group_of(step_id)
    out = [f"## Текущий шаг: {track.step_name(step_id)} (id {step_id})"
           + (f", этап «{group}»" if group else "")]
    if act and act.agent.agent_instructions:
        out.append(f"### Инструкции для агента\n{act.agent.agent_instructions}")
    if act and act.content.strip():
        out.append(f"### Описание шага (для пользователя)\n{links.html_to_text(act.content)}")
    props = [p for p in (act.properties if act else []) if not p.get("isUnused")]
    if props:
        out.append("### Свойства шага\n" + "\n".join(_prop_line(p, run) for p in props))
    if act and act.agent.requirements:
        done = run.confirmed.get(step_id, {})
        out.append("### Обязательные условия (без них дальше нельзя)\n" + "\n".join(
            f"- [{'x' if r.id in done else ' '}] {r.text} (id {r.id})" for r in act.agent.requirements))
    if engine.needs_jira(track, step_id) or (act and act.agent.jira):
        j = _jira_template(track, step_id, settings)
        created = engine.step_issues(run, step_id)
        need = [k for k in j.placeholders() if not engine.is_filled(run.facts.get(k))]
        lines = [f"Проект {j.project or settings.jira.default_project or '?'}, тип {j.issue_type}",
                 f"Заголовок: {render_template(j.summary, run.facts) or '(составь сам по шагу и данным)'}",
                 f"Описание: {render_template(j.description, run.facts) or '(составь сам)'}"]
        raw = act.metadata.get("jiraSendTask") if act else None
        if raw and not (act and act.agent.jira):
            lines.append("Настройки отправки из трека: " + json.dumps(raw, ensure_ascii=False)[:1500])
        if need:
            lines.append("Не хватает данных: " + ", ".join(need))
        lines.append("Создано: " + (", ".join(i.key for i in created) if created else "ещё нет"))
        out.append("### Задача Jira на этом шаге\n" + "\n".join(lines))
    decisions = bpmn.decisions_after(g, step_id)
    if decisions:
        props_all = track.props()
        rows = []
        for d in decisions:
            p = props_all.get(d["prop"], {"codeName": d["prop"]})
            val = d["value"]
            shown = next((l for v, l in engine.variants(p) if str(v) == str(val)), _fmt(val))
            rows.append(f"- если «{engine.prop_label(p)}» ({d['prop']}) = {shown} → «{' '.join(str(d['to']).split())}»"
                        + (f" [{d['label']}]" if d["label"] else ""))
        out.append("### Что дальше (развилка после шага)\n" + "\n".join(rows))
    else:
        nxt = bpmn.follow(g, step_id, run.facts)
        out.append("### Что дальше\n" + ("- трек завершится" if nxt.kind == "end" else
                                         f"- шаг «{track.step_name(nxt.task)}»" if nxt.task else "-"))
    missing = engine.missing_for_step(run, track, step_id)
    out.append("### Чего не хватает, чтобы завершить шаг\n" +
               ("\n".join(f"- {m}" for m in missing) if missing else "- всё готово"))
    out.append(_link_block(track, step_id, settings))
    return "\n\n".join(p for p in out if p)


def system_prompt(run: Run, track: Track, settings: Settings) -> str:
    parts = [BASE_PROMPT]
    if settings.agent.system_prompt:
        parts.append("Дополнительно от аналитика:\n" + settings.agent.system_prompt)
    parts.append(f"# Трек «{track.name}»\n{track.description}")
    if track.agent_instructions:
        parts.append("## Общие инструкции трека\n" + track.agent_instructions)
    visited = [v.step_id for v in run.visits]
    if visited:
        uniq = list(dict.fromkeys(visited))
        parts.append("## Пройденные шаги\n" + "\n".join(
            f"{'▶' if s == run.current_step else '✓'} {s}: {track.step_name(s)}" for s in uniq))
    now = datetime.now().astimezone()
    parts.append(f"## Пользователь\n{run.user_name or run.user_id}\n\n"
                 f"Сейчас: {now:%Y-%m-%d %H:%M}, {WEEKDAYS[now.weekday()]}")
    props = track.props()
    facts = "\n".join(f"- {k}{' (' + engine.prop_label(props[k]) + ')' if k in props else ''}: {_fmt(v)}"
                      for k, v in run.facts.items()) or "- пока ничего"
    parts.append("## Собранные данные (помни их и используй)\n" + facts)
    if run.jira_issues:
        parts.append("## Созданные задачи Jira\n" + "\n".join(
            f"- {i.key} ({i.summary}) шаг {i.step_id} {i.url}" for i in run.jira_issues))
    if run.current_step and run.current_step in track.graph.nodes:
        parts.append(step_brief(run, track, run.current_step, settings))
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def _norm(key: str) -> str:
    return re.sub(r"[^a-zа-я0-9]+", "_", str(key).lower()).strip("_")


def normalize_keys(data: Dict[str, Any], track: Track, step_id: Optional[str]
                   ) -> Tuple[Dict[str, Any], List[Tuple[str, str]]]:
    """Map keys the model invented onto property codeNames: exact code, property name or
    description, or an unambiguous suffix/prefix ("install_window" -> "window")."""
    props = track.props()
    act = track.activity(step_id)
    step_codes = [p.get("codeName") for p in (act.properties if act else []) if p.get("codeName")]
    by_norm: Dict[str, str] = {}
    for code in step_codes + list(props):
        p = props.get(code, {})
        for label in (code, p.get("name"), p.get("description")):
            if label:
                by_norm.setdefault(_norm(label), code)
    out: Dict[str, Any] = {}
    renamed = []
    for k, v in data.items():
        nk = _norm(k)
        target = by_norm.get(nk)
        for pool in (step_codes, list(props)):
            if target is not None:
                break
            hits = {c for c in pool if _norm(c) and (nk.endswith("_" + _norm(c))
                                                     or nk.startswith(_norm(c) + "_"))}
            target = hits.pop() if len(hits) == 1 else None
        target = target or k
        if target != k:
            renamed.append((k, target))
        out[target] = v
    return out, renamed


def _track_urls(track: Track) -> set:
    return {l.url for tid in track.activities for l in engine.step_links(track, tid)}


def run_tool(name: str, args: Dict[str, Any], run: Run, track: Track, settings: Settings) -> str:
    sid = run.current_step
    act = track.activity(sid)
    try:
        if name == "save_info":
            data = args.get("data") or {}
            if not isinstance(data, dict) or not data:
                return "ошибка: data должен быть непустым объектом"
            data, renamed = normalize_keys(data, track, sid)
            props = track.props()
            saved, rejected = {}, []
            for k, v in data.items():
                val, err = engine.coerce(props.get(k), v)
                if err:
                    rejected.append(f"{k} = {_fmt(v)} не принято: {err}")
                else:
                    saved[k] = val
            run.facts.update(saved)
            if args.get("remember") and saved:
                storage.update_profile(run.user_id, saved)
            if saved:
                engine.event(run, "Сохранено: " + ", ".join(
                    f"{engine.prop_label(props[k]) if k in props else k} = {_fmt(v)}"
                    for k, v in saved.items()))
            out = "сохранено" if saved else "ничего не сохранено"
            if renamed:
                out += "; ключи приведены к кодам свойств: " + ", ".join(f"{a} → {b}" for a, b in renamed)
            if rejected:
                out += ". " + "; ".join(rejected) + " — уточни у пользователя"
            missing = engine.missing_for_step(run, track) if act else []
            return out + (". Чтобы завершить шаг, ещё не хватает: " + "; ".join(missing) if missing
                          else ". Всё для завершения шага готово")
        if name == "confirm_requirement":
            if not act:
                return "ошибка: нет текущего шага"
            rid = args.get("requirement_id", "")
            req = next((r for r in act.agent.requirements if r.id == rid), None)
            if not req:
                return f"ошибка: у шага нет условия {rid}"
            run.confirmed.setdefault(sid, {})[rid] = args.get("evidence", "")
            engine.event(run, f"Условие выполнено: {req.text}")
            return "подтверждено"
        if name == "read_link":
            url = args.get("url", "")
            if url not in _track_urls(track):
                return "ошибка: можно читать только ссылки из описаний шагов трека"
            off = max(int(args.get("offset") or 0), 0)
            text = links.fetch_text(url, settings.links)
            chunk = text[off:off + settings.links.max_chars]
            rest = len(text) - off - len(chunk)
            return chunk + (f"\n…(осталось {rest} символов, offset={off + len(chunk)})" if rest > 0 else "")
        if name == "create_jira_issue":
            return _create_issue(args, run, track, settings)
        if name == "link_jira_issue":
            return _link_issue(args, run, track, settings)
        if name == "check_jira_status":
            info = jira.issue_status(settings.jira, args.get("issue_key", "").strip().upper())
            for i in run.jira_issues:
                if i.key == info["key"]:
                    i.status = info["status"]
            return json.dumps(info, ensure_ascii=False)
        if name == "complete_step":
            new = engine.complete(run, track, args.get("summary", ""))
            if new is None:
                return "трек завершён. Поздравь пользователя и кратко подведи итог."
            return "шаг завершён. Начни новый шаг:\n\n" + step_brief(run, track, new, settings)
        if name == "go_back":
            engine.go_back(run, track, args.get("step_id", ""))
            return "возврат выполнен:\n\n" + step_brief(run, track, run.current_step, settings)
        return f"ошибка: неизвестный инструмент {name}"
    except (engine.RuleError, jira.JiraError) as e:
        return f"отказано: {e}"
    except Exception as e:
        logger.exception("tool %s failed", name)
        return f"ошибка инструмента: {e}"


def _create_issue(args: Dict[str, Any], run: Run, track: Track, settings: Settings) -> str:
    sid = run.current_step
    act = track.activity(sid)
    if not act or not (engine.needs_jira(track, sid) or act.agent.jira):
        return "отказано: на текущем шаге не предусмотрено создание задачи Jira"
    j = _jira_template(track, sid, settings)
    need = [k for k in j.placeholders() if not engine.is_filled(run.facts.get(k))]
    if need:
        return "отказано: сначала узнай у пользователя и сохрани: " + ", ".join(need)
    summary = render_template(j.summary, run.facts) or args.get("summary", "").strip()
    if not summary:
        return "отказано: нужен заголовок задачи (summary)"
    description = render_template(j.description, run.facts)
    if args.get("description"):
        description = (description + "\n\n" + args["description"]).strip()
    res = jira.create_issue(settings.jira, j.project, j.issue_type, summary, description,
                            j.labels, j.priority, j.extra_fields)
    run.jira_issues.append(JiraIssueRef(key=res["key"], url=res["url"], summary=summary,
                                        step_id=sid, status="создана"))
    run.facts[f"jira_{sid}"] = res["key"]
    engine.event(run, f"Создана задача Jira {res['key']}")
    return json.dumps({"key": res["key"], "url": res["url"], "summary": summary}, ensure_ascii=False)


ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")


def _link_issue(args: Dict[str, Any], run: Run, track: Track, settings: Settings) -> str:
    sid = run.current_step
    act = track.activity(sid)
    if not act or not (engine.needs_jira(track, sid) or act.agent.jira):
        return "отказано: на текущем шаге задача Jira не нужна"
    key = str(args.get("issue_key", "")).strip().upper()
    if not ISSUE_KEY_RE.match(key):
        return "отказано: ключ задачи должен быть вида ПРОЕКТ-123"
    status, url, summary = "указана пользователем", "", str(args.get("summary") or "")
    configured = settings.jira.mode == "mock" or bool(settings.jira.base_url)
    if configured:
        try:
            info = jira.issue_status(settings.jira, key)  # make sure the issue exists
            status, url, summary = info["status"], info.get("url", ""), summary or info.get("summary", "")
        except jira.JiraError as e:
            if "не найдена" in str(e) or "404" in str(e):
                return f"отказано: задача {key} не найдена в Jira, проверь ключ у пользователя"
            logger.warning("jira check for %s failed: %s", key, e)
    run.jira_issues.append(JiraIssueRef(key=key, url=url, summary=summary, step_id=sid, status=status))
    run.facts[f"jira_{sid}"] = key
    engine.event(run, f"Привязана задача Jira {key}")
    return f"задача {key} привязана к шагу"


# ---------------------------------------------------------------------------
# Turn
# ---------------------------------------------------------------------------

def _history(run: Run, limit: int) -> List[Dict[str, Any]]:
    msgs = [m for m in run.messages if m.role in ("user", "assistant")][-limit:]
    while msgs and msgs[0].role == "assistant":  # the model expects a user turn first
        msgs = msgs[1:]
    return [{"role": m.role, "content": m.text} for m in msgs]


def _step_ready(run: Run, track: Track) -> bool:
    return run.current_step in track.graph.nodes and not engine.missing_for_step(run, track)


def run_turn(run: Run, user_text: Optional[str], settings: Settings,
             chat: Optional[Callable[..., Dict[str, Any]]] = None) -> Run:
    """Handle one user message (None = the run just started / was resumed)."""
    chat = chat or llm.chat
    if run.status != RunStatus.active:
        raise engine.RuleError(f"прохождение уже {run.status.value}")
    track = engine.track_for_run(run)
    if user_text:
        run.messages.append(ChatMessage(role="user", text=user_text, step_id=run.current_step))
    convo = _history(run, settings.agent.history_limit)
    if not user_text:
        convo.append({"role": "user", "content": START_PROMPT if len(run.messages) <= 1
                      else RESUME_PROMPT})
    reply = ""
    start_step = run.current_step
    was_ready = _step_ready(run, track)
    nudged = False
    for _ in range(MAX_ROUNDS):
        messages = [{"role": "system", "content": system_prompt(run, track, settings)}] + convo
        try:
            msg = chat(settings.agent, messages, tools=TOOLS, model=track.model)
        except llm.LLMError as e:
            engine.event(run, f"⚠️ Агент недоступен: {e}")
            return run
        calls = msg.get("tool_calls") or []
        if not calls:
            reply = (msg.get("content") or "").strip()
            # the step became complete during this turn, but the model only *said* it moves on
            if (not nudged and run.status == RunStatus.active and run.current_step == start_step
                    and not was_ready and _step_ready(run, track)):
                nudged = True
                convo.append({"role": "assistant", "content": reply})
                convo.append({"role": "user", "content": NUDGE_PROMPT})
                continue
            break
        convo.append({"role": "assistant", "content": msg.get("content") or "",
                      "tool_calls": calls})
        for c in calls:
            fn = c.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = run_tool(fn.get("name", ""), args, run, track, settings)
            convo.append({"role": "tool", "tool_call_id": c.get("id", ""),
                          "name": fn.get("name", ""), "content": result})
        if run.status != RunStatus.active and msg.get("content"):
            reply = msg["content"].strip()
    if not reply:
        reply = ("Трек пройден. Спасибо!" if run.status == RunStatus.completed
                 else "Не удалось сформулировать ответ, повторите, пожалуйста, сообщение.")
    run.messages.append(ChatMessage(role="assistant", text=reply, step_id=run.current_step))
    return run


# ---------------------------------------------------------------------------
# Track draft from a text description (analyst helper)
# ---------------------------------------------------------------------------

DRAFT_PROMPT = """Ты помогаешь аналитику составить трек — пошаговый процесс, по которому ИИ-агент ведёт пользователя.
Верни только JSON:
{"id": "latin-slug", "name": "...", "description": "...", "agent_instructions": "...",
 "steps": [{"id": "s1", "name": "Название шага", "type": "task" или "jira-send",
            "content": "<p>Что сделать пользователю (HTML: p, ol, ul, li, strong, a)</p>",
            "agent_instructions": "что проверить агенту",
            "properties": [{"codeName": "snake_case", "name": "Название", "description": "Вопрос пользователю",
                            "valueType": "string|boolean|number|link|date", "valueVariants": [], "required": true}],
            "requirements": [{"id": "r1", "text": "условие, без которого дальше нельзя"}],
            "next": []}]}
Правила: 3–12 шагов по порядку; next пустой = следующий по списку шаг (у последнего — завершение).
Ветвление: next = [{"to": "id шага или end", "prop": "codeName", "value": true, "name": "Да"}, {"to": ..., "prop": ..., "value": false, "name": "Нет"}];
свойство, по которому ветвимся, объяви в этом же шаге (boolean или с valueVariants — тогда value равно одному из вариантов).
Шаги, где нужно создать заявку в Jira, помечай type "jira-send"."""


def draft_track(description: str, settings: Settings,
                chat: Optional[Callable[..., Dict[str, Any]]] = None) -> Track:
    chat = chat or llm.chat
    msg = chat(settings.agent, [{"role": "system", "content": DRAFT_PROMPT},
                                {"role": "user", "content": description}], json_mode=True)
    text = re.sub(r"^```(?:json)?|```$", "", (msg.get("content") or "").strip()).strip()
    data = json.loads(text)
    meta = {"id": re.sub(r"[^a-zA-Z0-9_-]+", "-", str(data.get("id") or "draft")).strip("-") or "draft",
            "name": data.get("name") or "Новый трек", "description": data.get("description", ""),
            "agent_instructions": data.get("agent_instructions", "")}
    steps = data.get("steps") or []
    if not steps:
        raise ValueError("в черновике нет шагов")
    return tracks.from_spec(meta, steps)
