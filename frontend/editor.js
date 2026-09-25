// Track editor: the BPMN scheme (bpmn-js) + a panel for the selected element.
// Tracks are stored in the partner system's format: the scheme decides which steps exist and how
// they connect; each step's activity (metadata/properties/attachments + our agent.json) is edited
// in the panel. Unknown partner fields are kept untouched.
"use strict";

let ed = null;

const VALUE_TYPES = { string: "Строка", text: "Текст", boolean: "Да / нет", number: "Число",
  date: "Дата", link: "Ссылка", select: "Выбор из вариантов" };
const AUDIENCE_RU = { both: "Агенту и пользователю", user: "Пользователю", agent: "Только агенту" };
const TASK_TYPES_RU = { task: "Задача", "jira-send": "Отправка в Jira" };
const COND_RE = /^\$\{\s*objProps\.prop\(\s*"([^"]+)"\s*\)\.value\(\)\s*(==|!=)\s*(true|false|"[^"]*"|-?\d+(?:\.\d+)?)\s*\}$/;

const dirty = () => !!ed && ed.dirty;

async function renderEditor(id) {
  const view = await load(() => api("/tracks/" + id));
  openEditor(view, false);
}

function openEditor(view, isNew) {
  if (ed?.modeler) ed.modeler.destroy();
  const t = view.track;
  ed = {
    meta: { id: t.id, name: t.name, description: t.description || "", agent_instructions: t.agent_instructions || "", model: t.model || "" },
    bpmn: t.bpmn, tasks: t.tasks || [], activities: JSON.parse(JSON.stringify(t.activities || {})),
    taskTypes: {}, status: t.status || "draft", version: t.version, versions: view.versions || [],
    lint: view.lint || { errors: [], warnings: [] }, isNew, dirty: isNew, tab: "scheme", sel: null, modeler: null,
  };
  if (isNew) history.replaceState(null, "", "#/tracks/new");
  drawEditor();
}

function markDirty() {
  if (!ed) return;
  ed.dirty = true;
  document.getElementById("saveBtn")?.removeAttribute("disabled");
  lintSoon();
}

// ---------------------------------------------------------------------------
// Layout
// ---------------------------------------------------------------------------

function drawEditor() {
  mount(app,
    h("div", { id: "edHead" }),
    h("div", { id: "lintBox" }),
    h("div.tabs", { id: "edTabs" }),
    h("div", { id: "tab-scheme" },
      h("div.bpmn-shell",
        h("div.card.bpmn-canvas", { id: "canvas" }),
        h("aside.card.bpmn-panel", { id: "panel" }))),
    h("div.hidden", { id: "tab-general" }),
    h("div.hidden", { id: "tab-xml" }));
  drawHead(); drawTabs(); drawLint(); initModeler();
}

function drawHead() {
  const st = ed.status, actions = [];
  if (!ed.isNew) {
    actions.push(h("button", { onclick: () => location.hash = "#/stats/" + ed.meta.id }, "Статистика"));
    actions.push(h("button", { onclick: exportMenu }, "Экспорт"));
    if (st === "published") actions.push(h("button", { onclick: () => setStatus("unpublish") }, "Снять с публикации"));
    else actions.push(h("button", { onclick: () => setStatus("publish") }, "Опубликовать"));
    if (st !== "archived") actions.push(h("button", { onclick: () => setStatus("archive") }, "В архив"));
    if (st !== "published") actions.push(h("button.danger", { onclick: deleteTrack }, "Удалить"));
  }
  actions.push(h("button.primary", { id: "saveBtn", onclick: saveTrack, disabled: !ed.dirty }, ed.isNew ? "Создать трек" : "Сохранить"));
  const versionSel = ed.versions.length > 1 ? h("select", { style: { width: "auto" }, onchange: e => loadVersion(Number(e.target.value)) },
    ed.versions.slice().reverse().map(v => h("option", { value: v, selected: v === ed.version }, `v${v}` + (v === ed.versions.at(-1) ? " (последняя)" : "")))) : null;
  mount(document.getElementById("edHead"), pageHead(
    h("div", h("div.row.wrap", h("a", { href: "#/tracks" }, "Треки"), h("span.muted", "/"), h("h1", ed.meta.name || "Без названия"),
      ed.isNew ? h("span.badge.warn", "не сохранён") : badge(st), versionSel),
      st === "published" ? h("div.small.muted", "Изменения опубликованного трека сразу получат новые прохождения. Начатые прохождения идут по своей версии.") : null),
    actions));
}

function drawTabs() {
  const tabs = [["scheme", "Схема и шаги"], ["general", "Общее"], ["xml", "BPMN XML"]];
  mount(document.getElementById("edTabs"), tabs.map(([k, l]) =>
    h("button" + (ed.tab === k ? ".on" : ""), { onclick: () => switchTab(k) }, l)));
}

async function switchTab(tab) {
  ed.tab = tab;
  drawTabs();
  for (const k of ["scheme", "general", "xml"]) document.getElementById("tab-" + k).classList.toggle("hidden", k !== tab);
  if (tab === "general") drawGeneral();
  if (tab === "xml") await drawXml();
}

function drawLint() {
  const box = document.getElementById("lintBox");
  if (!box) return;
  const { errors, warnings } = ed.lint;
  const list = items => h("ul", items.slice(0, 12).map(e => h("li", e)), items.length > 12 ? h("li", `…и ещё ${items.length - 12}`) : null);
  mount(box,
    errors.length ? h("details.lint.errors", h("summary", h("strong", `Ошибки: ${errors.length}`), " — мешают публикации"), list(errors)) : null,
    warnings.length ? h("details.lint.warnings", h("summary", h("strong", `Стоит проверить: ${warnings.length}`)), list(warnings)) : null);
}

// ---------------------------------------------------------------------------
// Modeler
// ---------------------------------------------------------------------------

async function initModeler() {
  if (typeof BpmnJS === "undefined") {
    mount(document.getElementById("canvas"), h("div.empty", "Не загрузился редактор схем (vendor/bpmn-js)."));
    return;
  }
  const modeler = new BpmnJS({ container: document.getElementById("canvas") });
  ed.modeler = modeler;
  try {
    await modeler.importXML(ed.bpmn);
    fitReadable(modeler);
  } catch (e) {
    mount(document.getElementById("canvas"), h("div.empty", "Схема не открывается: " + e.message));
    return;
  }
  modeler.on("selection.changed", e => { ed.sel = e.newSelection.length === 1 ? e.newSelection[0] : null; drawPanel(); });
  modeler.on("commandStack.changed", () => { markDirty(); drawOverlays(); });
  modeler.on("shape.added", e => { if (isTask(e.element)) activity(e.element.id); });
  drawOverlays();
  drawPanel();
}

// fit the whole scheme, but long partner schemes would become unreadable: then start at the start event
function fitReadable(modeler) {
  const canvas = modeler.get("canvas");
  canvas.zoom("fit-viewport", "auto");
  if (canvas.zoom() >= 0.55) return;
  canvas.zoom(0.75);
  const start = modeler.get("elementRegistry").filter(e => bo(e)?.$type === "bpmn:StartEvent")[0];
  if (start) {
    const vb = canvas.viewbox();
    canvas.viewbox({ x: start.x - 130, y: start.y + start.height / 2 - vb.height / 2, width: vb.width, height: vb.height });
  }
}

const bo = el => el.businessObject;
const isTask = el => /Task$/.test(bo(el)?.$type || "") || bo(el)?.$type === "bpmn:Task";
const typeOf = id => ed.taskTypes[id] || ed.activities[id]?.metadata?.type || "task";

function activity(id) {
  if (!ed.activities[id]) {
    const el = ed.modeler?.get("elementRegistry").get(id);
    ed.activities[id] = { metadata: { name: el ? (bo(el).name || "") : "", codeName: id, content: "", type: "task", properties: [] },
      properties: [], attachments: [], agent: {} };
  }
  const a = ed.activities[id];
  a.agent = Object.assign({ agent_instructions: "", requirements: [], jira: null, link_audience: {}, extra_links: [] }, a.agent || {});
  return a;
}

function drawOverlays() {
  const overlays = ed.modeler.get("overlays");
  overlays.remove({ type: "track" });
  ed.modeler.get("elementRegistry").filter(isTask).forEach(el => {
    const a = ed.activities[el.id];
    const badges = [];
    if (typeOf(el.id) === "jira-send") badges.push(`<span class="ov ov-jira">Jira</span>`);
    const props = (a?.properties || []).filter(p => !p.isUnused).length;
    if (props) badges.push(`<span class="ov">✎ ${props}</span>`);
    if (!a || !(a.metadata?.content || "").trim()) badges.push(`<span class="ov ov-warn" title="нет описания">!</span>`);
    if (badges.length) overlays.add(el.id, "track", { position: { bottom: 8, left: 4 }, html: `<div class="ov-row">${badges.join("")}</div>` });
  });
}

function allProps() {
  const out = {};
  for (const [tid, a] of Object.entries(ed.activities)) {
    for (const p of a.properties || []) if (p.codeName && !out[p.codeName]) out[p.codeName] = { ...p, _task: tid };
  }
  return out;
}

const propLabel = p => p.description || p.name || p.codeName;
const variantsOf = p => (p.valueVariants || []).map(v => typeof v === "object" && v
  ? { value: v.value ?? v.code ?? v.codeName ?? v.id ?? v.name, label: String(v.label ?? v.name ?? v.title ?? v.value) }
  : { value: v, label: String(v) });

// ---------------------------------------------------------------------------
// Panel
// ---------------------------------------------------------------------------

function section(title, desc, ...children) {
  return h("div.panel-section", h("div.block-head", h("div", h("h3", title), desc ? h("div.desc", desc) : null)), children);
}

function drawPanel() {
  const panel = document.getElementById("panel");
  if (!panel || !ed) return;
  const el = ed.sel;
  if (!el) return mount(panel, processPanel());
  const type = bo(el).$type;
  if (isTask(el)) return mount(panel, taskPanel(el));
  if (type === "bpmn:SequenceFlow") return mount(panel, flowPanel(el));
  if (/Gateway$/.test(type)) return mount(panel, gatewayPanel(el));
  if (type === "bpmn:Group") return mount(panel, groupPanel(el));
  mount(panel, h("div.panel-section", h("h3", type.replace("bpmn:", "")),
    h("p.small.muted", type === "bpmn:StartEvent" ? "Начало трека: от него агент идёт к первому шагу."
      : type === "bpmn:EndEvent" ? "Конец трека: когда агент доходит сюда, прохождение завершено."
      : "Этот элемент движок пропускает.")));
}

function processPanel() {
  const reg = ed.modeler.get("elementRegistry");
  const tasks = reg.filter(isTask), gws = reg.filter(e => /Gateway$/.test(bo(e).$type));
  return h("div",
    h("div.panel-section", h("h3", "Трек"),
      h("p.small.muted", `${tasks.length} шагов, ${gws.length} шлюзов. Выберите шаг, переход или шлюз на схеме, чтобы настроить его.`),
      h("ul.small.muted.help",
        h("li", "Новый шаг: инструмент «задача» в палитре слева, потом связь от предыдущего элемента."),
        h("li", "Ветвление: ромб-шлюз и несколько переходов; условие перехода задаётся здесь, на панели."),
        h("li", "Этап: инструмент «группа» — обведите шаги и дайте ей название."))));
}

function taskPanel(el) {
  const id = el.id, a = activity(id);
  const name = h("input", { value: bo(el).name || "" });
  name.addEventListener("change", () => { ed.modeler.get("modeling").updateLabel(el, name.value.trim()); a.metadata.name = name.value.trim(); });
  const typeSel = h("select", Object.entries(TASK_TYPES_RU).map(([v, l]) => h("option", { value: v, selected: typeOf(id) === v }, l)));
  typeSel.addEventListener("change", () => { ed.taskTypes[id] = typeSel.value; a.metadata.type = typeSel.value; markDirty(); drawOverlays(); drawPanel(); });
  const group = groupName(el);
  return h("div",
    h("div.panel-section",
      h("label.field", h("span", "Название шага"), name),
      h("div.grid-2", { style: { marginTop: "10px" } },
        h("label.field", h("span", "Тип"), typeSel),
        h("label.field", h("span", "Этап"), h("input", { value: group || "—", disabled: true }))),
      h("div.hint", "id ", h("code", id))),
    section("Описание шага", "Инструкция для пользователя, как в партнёрской системе. Ссылки агент откроет и прочитает.", richText(a)),
    section("Свойства", "Что пользователь должен указать. По кодам свойств работают условия переходов.", propsEditor(a)),
    agentSection(id, a),
    section("Оценка времени", null, timeEstimate(a)),
    rawMetadata(a));
}

function groupName(el) {
  const c = el.x + el.width / 2, m = el.y + el.height / 2;
  const g = ed.modeler.get("elementRegistry").filter(e => bo(e).$type === "bpmn:Group")
    .find(g => c >= g.x && c <= g.x + g.width && m >= g.y && m <= g.y + g.height);
  return g ? bo(g).categoryValueRef?.value || "" : "";
}

// --- rich text (tiptap-compatible HTML: p, strong, em, ol/ul/li, a) ---------

function richText(a) {
  const area = h("div.rte", { contenteditable: "true", html: safeHtml(a.metadata.content || "") });
  const sync = () => { a.metadata.content = cleanRte(area.innerHTML); markDirty(); drawOverlays(); };
  area.addEventListener("input", sync);
  const cmd = (c, v) => e => { e.preventDefault(); area.focus(); document.execCommand(c, false, v); sync(); };
  const link = e => {
    e.preventDefault();
    const url = prompt("Ссылка (https://…)");
    if (url && /^https?:\/\//.test(url)) { area.focus(); document.execCommand("createLink", false, url); sync(); }
  };
  return h("div.rte-wrap",
    h("div.rte-bar",
      h("button.sm.ghost", { title: "Жирный", onmousedown: cmd("bold") }, h("b", "Ж")),
      h("button.sm.ghost", { title: "Курсив", onmousedown: cmd("italic") }, h("i", "К")),
      h("button.sm.ghost", { title: "Нумерованный список", onmousedown: cmd("insertOrderedList") }, "1."),
      h("button.sm.ghost", { title: "Список", onmousedown: cmd("insertUnorderedList") }, "•"),
      h("button.sm.ghost", { title: "Ссылка", onmousedown: link }, h("u", "ссылка")),
      h("button.sm.ghost", { title: "Очистить форматирование", onmousedown: cmd("removeFormat") }, "⌫")),
    area);
}

function cleanRte(html) {
  const s = safeHtml(html.replace(/<div>/g, "<p>").replace(/<\/div>/g, "</p>"));
  return s === "<p><br></p>" ? "" : s;
}

// --- properties (properties.json) -----------------------------------------------

function propsEditor(a) {
  const rows = h("div.list-rows");
  const redraw = () => mount(rows, (a.properties || []).map((p, i) => propRow(a, p, i, redraw)));
  redraw();
  return h("div", rows,
    h("button.sm.add-row", { onclick: () => {
      a.properties.push({ id: crypto.getRandomValues ? randomUuid() : String(Date.now()), name: "", codeName: "", description: "",
        isArtifact: false, isEditableInTaskOnly: true, requiredToFillOut: true, valueType: "string", valueVariants: [],
        trackId: trackUuid(), isUnused: false, _new: true });
      markDirty(); redraw(); drawOverlays();
    } }, "+ свойство"));
}

function trackUuid() {
  for (const a of Object.values(ed.activities)) for (const p of a.properties || []) if (p.trackId) return p.trackId;
  return "";
}

function randomUuid() {
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
  const x = Array.from(b, v => v.toString(16).padStart(2, "0")).join("");
  return `${x.slice(0, 8)}-${x.slice(8, 12)}-${x.slice(12, 16)}-${x.slice(16, 20)}-${x.slice(20)}`;
}

function propRow(a, p, i, redraw) {
  const bind = (key, el, conv = v => v) => { el.addEventListener("input", () => { p[key] = conv(el.value); markDirty(); }); return el; };
  const code = bind("codeName", h("input", { value: p.codeName, placeholder: "код", style: { fontFamily: "var(--mono)" } }));
  const name = h("input", { value: p.name, placeholder: "Название" });
  name.addEventListener("input", () => {
    p.name = name.value;
    if (p._new && !p._codeEdited) { p.codeName = slugify(name.value).replace(/-/g, "_"); code.value = p.codeName; }
    markDirty();
  });
  code.addEventListener("input", () => { p._codeEdited = true; });
  const type = h("select", Object.entries(VALUE_TYPES).map(([v, l]) => h("option", { value: v, selected: (p.valueType || "string") === v }, l)));
  if (!(p.valueType in VALUE_TYPES)) type.append(h("option", { value: p.valueType, selected: true }, p.valueType));
  type.addEventListener("change", () => { p.valueType = type.value; markDirty(); redraw(); });
  const objVariants = (p.valueVariants || []).some(v => v && typeof v === "object");
  let variants = null;
  if (objVariants) {
    variants = h("div.small.muted", "Варианты заданы в партнёрской системе: " + variantsOf(p).map(v => `${v.label} (${v.value})`).join(", "));
  } else if (type.value === "select" || (p.valueVariants || []).length) {
    variants = h("input", { value: (p.valueVariants || []).join(", "), placeholder: "варианты через запятую: dev, up" });
    variants.addEventListener("input", () => { p.valueVariants = variants.value.split(",").map(s => s.trim()).filter(Boolean); markDirty(); });
  }
  const req = h("input", { type: "checkbox", checked: !!p.requiredToFillOut });
  req.addEventListener("change", () => { p.requiredToFillOut = req.checked; markDirty(); });
  return h("div.prop-card",
    h("div.grid-2", name, code),
    bind("description", h("input", { value: p.description || "", placeholder: "Вопрос / подсказка пользователю" })),
    h("div.row", type, h("label.check", req, "обязательно"), h("span.spacer"),
      h("button.ghost.sm.icon.danger", { title: "Удалить свойство", onclick: () => { a.properties.splice(i, 1); markDirty(); redraw(); drawOverlays(); } }, "✕")),
    variants);
}

// --- agent.json ---------------------------------------------------------------

function agentSection(id, a) {
  const ag = a.agent;
  const instr = h("textarea", { rows: 3, value: ag.agent_instructions, placeholder: "Что агенту проверить или уточнить на этом шаге" });
  instr.addEventListener("input", () => { ag.agent_instructions = instr.value; markDirty(); });
  const reqRows = h("div.list-rows");
  const drawReqs = () => mount(reqRows, ag.requirements.map((r, i) => {
    const t = h("input", { value: r.text, placeholder: "Например: подписан договор" });
    t.addEventListener("input", () => { r.text = t.value; markDirty(); });
    return h("div.list-row.req", t, h("button.ghost.sm.icon.danger", { onclick: () => { ag.requirements.splice(i, 1); markDirty(); drawReqs(); } }, "✕"));
  }));
  drawReqs();
  const addReq = h("button.sm.add-row", { onclick: () => {
    let n = ag.requirements.length + 1;
    while (ag.requirements.some(r => r.id === "r" + n)) n++;
    ag.requirements.push({ id: "r" + n, text: "" }); markDirty(); drawReqs();
  } }, "+ условие");

  const links = contentLinks(a.metadata.content || "");
  const linkRows = links.map(([title, url]) => {
    const s = h("select", Object.entries(AUDIENCE_RU).map(([v, l]) => h("option", { value: v, selected: (ag.link_audience[url] || "both") === v }, l)));
    s.addEventListener("change", () => { if (s.value === "both") delete ag.link_audience[url]; else ag.link_audience[url] = s.value; markDirty(); });
    const box = h("div.preview-box.hidden");
    return h("div", h("div.link-aud", h("a", { href: url, target: "_blank", rel: "noopener", title: url }, title || url), s,
      h("button.sm", { onclick: () => previewLink(url, box) }, "Проверить")), box);
  });

  return h("div",
    section("Для агента", "Хранится отдельно (agent.json), партнёрская система это не видит.",
      h("label.field", h("span", "Инструкции агенту"), instr),
      h("div.field-title", "Обязательные условия"), reqRows, addReq,
      links.length ? h("div", h("div.field-title", "Ссылки из описания"), linkRows) : null,
      jiraBlock(id, a)));
}

function contentLinks(html) {
  const doc = new DOMParser().parseFromString(html, "text/html");
  return [...doc.querySelectorAll("a[href]")]
    .filter(x => !/userlink|user-mention/.test(x.className) && !/\/display\/~|viewuserprofile/.test(x.getAttribute("href")))
    .map(x => [x.textContent.trim(), x.getAttribute("href")]);
}

function jiraBlock(id, a) {
  const isSend = typeOf(id) === "jira-send";
  const ag = a.agent;
  if (!isSend && !ag.jira) {
    return h("div.field-title", h("button.sm", { onclick: () => { ag.jira = { project: "", issue_type: "Task", summary: "", description: "", labels: [], required: false }; markDirty(); drawPanel(); } }, "+ шаблон задачи Jira"));
  }
  const j = ag.jira || {};
  const set = (key, el) => { el.addEventListener("input", () => { ag.jira = Object.assign(ag.jira || { issue_type: "Task", labels: [], required: true }, { [key]: el.value }); markDirty(); }); return el; };
  const codes = Object.keys(allProps());
  let last = null;
  const summary = set("summary", h("input", { value: j.summary || "", placeholder: "Установка {team} {version}" }));
  const desc = set("description", h("textarea", { rows: 3, value: j.description || "", placeholder: "Команда: {team}" }));
  [summary, desc].forEach(el => el.addEventListener("focus", () => { last = el; }));
  const insert = code => {
    if (!last) return toast("Поставьте курсор в заголовок или описание");
    const pos = last.selectionStart ?? last.value.length, tok = `{${code}}`;
    last.value = last.value.slice(0, pos) + tok + last.value.slice(last.selectionEnd ?? pos);
    last.dispatchEvent(new Event("input")); last.focus();
  };
  return h("div",
    h("div.field-title", "Задача Jira", isSend ? h("span.muted.small", " — шаг отправляет задачу, без неё дальше нельзя") : null),
    a.metadata.jiraSendTask ? h("div.hint", "Настройки отправки из партнёрской системы сохранены и передаются агенту как подсказка.") : null,
    h("div.grid-2",
      h("label.field", h("span", "Проект"), set("project", h("input", { value: j.project || "", placeholder: "по умолчанию из настроек" }))),
      h("label.field", h("span", "Тип задачи"), set("issue_type", h("input", { value: j.issue_type || "Task" })))),
    h("label.field", h("span", "Заголовок"), summary),
    h("label.field", h("span", "Описание"), desc),
    codes.length ? h("div.placeholders", codes.map(c => h("span.chip", { onclick: () => insert(c) }, `{${c}}`))) : null,
    !isSend ? h("button.sm.danger", { onclick: () => { ag.jira = null; markDirty(); drawPanel(); } }, "Убрать шаблон") : null);
}

function timeEstimate(a) {
  const te = a.metadata.timeEstimate || (a.metadata.timeEstimate = { quantity: 0, unit: "minutes" });
  const q = h("input", { type: "number", min: 0, value: te.quantity ?? 0, style: { width: "100px" } });
  q.addEventListener("input", () => { te.quantity = Number(q.value) || 0; markDirty(); });
  const u = h("select", { style: { width: "auto" } }, [["minutes", "минут"], ["hours", "часов"], ["days", "дней"]].map(([v, l]) => h("option", { value: v, selected: te.unit === v }, l)));
  if (!["minutes", "hours", "days"].includes(te.unit)) u.append(h("option", { value: te.unit, selected: true }, te.unit));
  u.addEventListener("change", () => { te.unit = u.value; markDirty(); });
  return h("div.row", q, u);
}

function rawMetadata(a) {
  const known = new Set(["id", "name", "codeName", "content", "format", "displayOrder", "type", "properties", "attachments", "timeEstimate"]);
  const other = Object.fromEntries(Object.entries(a.metadata).filter(([k]) => !known.has(k)));
  return h("details.panel-section", h("summary.small.muted", "Остальные поля metadata.json (сохраняются как есть)"),
    h("pre.preview-box", JSON.stringify(other, null, 1)),
    (a.attachments || []).length ? h("div.small.muted", `Вложений: ${a.attachments.length}`) : null);
}

// --- flows / gateways / groups -------------------------------------------------

function parseCond(text) {
  const m = COND_RE.exec((text || "").trim());
  if (!m) return null;
  const raw = m[3];
  const value = raw === "true" ? true : raw === "false" ? false : raw.startsWith('"') ? raw.slice(1, -1) : Number(raw);
  return { prop: m[1], op: m[2], value };
}

function formatCond(prop, value) {
  const lit = typeof value === "boolean" ? String(value) : typeof value === "number" ? String(value) : JSON.stringify(String(value));
  return `\${objProps.prop("${prop}").value() == ${lit}}`;
}

function flowPanel(el) {
  const modeling = ed.modeler.get("modeling");
  const src = el.source, fromGateway = /Gateway$/.test(bo(src).$type);
  const name = h("input", { value: bo(el).name || "", placeholder: "Подпись на схеме, например «Да»" });
  name.addEventListener("change", () => modeling.updateLabel(el, name.value.trim()));
  const text = bo(el).conditionExpression?.body || "";
  const cond = parseCond(text);
  const props = allProps();
  const propSel = h("select", h("option", { value: "" }, "— без условия —"),
    Object.values(props).map(p => h("option", { value: p.codeName, selected: cond?.prop === p.codeName }, `${propLabel(p)} (${p.codeName})`)));
  if (cond && !props[cond.prop]) propSel.append(h("option", { value: cond.prop, selected: true }, `${cond.prop} — нет такого свойства`));
  const valueBox = h("div");
  const apply = value => {
    if (!propSel.value) { modeling.updateProperties(el, { conditionExpression: undefined }); return; }
    const expr = ed.modeler.get("moddle").create("bpmn:FormalExpression", { body: formatCond(propSel.value, value) });
    modeling.updateProperties(el, { conditionExpression: expr });
  };
  const drawValue = () => {
    const p = props[propSel.value];
    if (!propSel.value) { mount(valueBox); apply(); return; }
    const cur = cond && cond.prop === propSel.value ? cond.value : undefined;
    let input;
    const vs = p ? variantsOf(p) : [];
    if (p && (p.valueType === "boolean" || typeof cur === "boolean") && !vs.length) {
      input = h("select", h("option", { value: "true", selected: cur !== false }, "Да (true)"), h("option", { value: "false", selected: cur === false }, "Нет (false)"));
      input.addEventListener("change", () => apply(input.value === "true"));
      if (cur === undefined) apply(true);
    } else if (vs.length) {
      input = h("select", vs.map(v => h("option", { value: String(v.value), selected: String(cur) === String(v.value) }, `${v.label}`)));
      input.addEventListener("change", () => apply(vs.find(v => String(v.value) === input.value).value));
      if (cur === undefined) apply(vs[0].value);
    } else {
      input = h("input", { value: cur ?? "", placeholder: "значение" });
      input.addEventListener("change", () => apply(p?.valueType === "number" && input.value !== "" ? Number(input.value) : input.value));
    }
    mount(valueBox, h("label.field", h("span", "равно"), input));
  };
  propSel.addEventListener("change", drawValue);
  if (cond) drawValue();
  return h("div",
    h("div.panel-section", h("h3", "Переход"),
      h("div.small.muted", `${nodeName(src)} → ${nodeName(el.target)}`),
      h("label.field", { style: { marginTop: "10px" } }, h("span", "Подпись"), name)),
    fromGateway ? section("Условие ветки", "Агент пойдёт по этой ветке, если свойство имеет указанное значение.",
      h("label.field", h("span", "Свойство"), propSel), valueBox,
      text && !cond ? h("div.lint.errors", "Условие не в формате objProps: " + text) : null,
      text ? h("div.hint", h("code", text)) : null)
      : h("div.panel-section.small.muted", "Условия задаются только на переходах из шлюза."));
}

function nodeName(el) {
  const t = bo(el).$type;
  return bo(el).name || (/Gateway$/.test(t) ? "шлюз" : t === "bpmn:StartEvent" ? "старт" : t === "bpmn:EndEvent" ? "конец" : el.id);
}

function gatewayPanel(el) {
  const outs = el.outgoing || [];
  const sel = ed.modeler.get("selection");
  return h("div",
    h("div.panel-section", h("h3", "Шлюз"),
      h("p.small.muted", outs.length > 1 ? "Развилка: агент выберет ветку по значению свойства. Выберите ветку, чтобы задать условие."
        : "Слияние веток: просто пропускает дальше.")),
    outs.length > 1 ? h("div.panel-section", outs.map(f => {
      const c = parseCond(bo(f).conditionExpression?.body);
      const p = c ? allProps()[c.prop] : null;
      return h("div.branch-item", { onclick: () => sel.select(f) },
        h("strong", bo(f).name || "без подписи"), " → ", nodeName(f.target),
        h("div.small.muted", c ? `если «${p ? propLabel(p) : c.prop}» = ${c.value === true ? "да" : c.value === false ? "нет" : c.value}` : "без условия"));
    })) : null);
}

function groupPanel(el) {
  const name = h("input", { value: bo(el).categoryValueRef?.value || "" });
  name.addEventListener("change", () => ed.modeler.get("modeling").updateLabel(el, name.value.trim()));
  return h("div.panel-section", h("h3", "Этап (группа)"), h("label.field", h("span", "Название"), name),
    h("p.small.muted", "Шаги внутри рамки относятся к этому этапу — пользователь видит этапы в прогрессе."));
}

// ---------------------------------------------------------------------------
// General / XML tabs
// ---------------------------------------------------------------------------

function drawGeneral() {
  const m = ed.meta;
  const bind = (key, el) => { el.addEventListener("input", () => { m[key] = el.value; markDirty(); if (key === "name") drawHead(); }); return el; };
  mount(document.getElementById("tab-general"), h("div.card.pad.stack",
    h("div.grid-2",
      h("label.field", h("span", "Название"), bind("name", h("input", { value: m.name }))),
      h("label.field", h("span", "Идентификатор"), h("input", { value: m.id, disabled: true }))),
    h("label.field", h("span", "Описание для пользователей"), bind("description", h("textarea", { rows: 3, value: m.description }))),
    h("label.field", h("span", "Общие инструкции агенту"), bind("agent_instructions", h("textarea", { rows: 5, value: m.agent_instructions, placeholder: "Действуют на всех шагах" }))),
    h("label.field", h("span", "Модель для этого трека"), bind("model", h("input", { value: m.model, placeholder: "пусто — модель из настроек" })))));
}

async function drawXml() {
  const { xml } = await ed.modeler.saveXML({ format: true });
  const area = h("textarea", { rows: 28, value: xml, style: { fontFamily: "var(--mono)", fontSize: "12px" } });
  mount(document.getElementById("tab-xml"), h("div.card.pad.stack",
    h("div.row", h("span.small.muted", "Схема в формате BPMN 2.0, как в партнёрской системе."), h("span.spacer"),
      h("button.primary", { onclick: async () => {
        try { await ed.modeler.importXML(area.value); markDirty(); drawOverlays(); toast("Схема применена, не забудьте сохранить"); switchTab("scheme"); }
        catch (e) { toast("Схема не открывается: " + e.message, true); }
      } }, "Применить")),
    area));
}

// ---------------------------------------------------------------------------
// Save, lint, status
// ---------------------------------------------------------------------------

async function payload() {
  const { xml } = await ed.modeler.saveXML({ format: true });
  const ids = new Set(ed.modeler.get("elementRegistry").filter(isTask).map(e => e.id));
  const activities = {};
  for (const [id, a] of Object.entries(ed.activities)) {
    if (!ids.has(id)) continue;
    activities[id] = { ...a, properties: (a.properties || []).filter(p => p.codeName).map(({ _new, _codeEdited, ...p }) => p),
      agent: { ...a.agent, requirements: (a.agent.requirements || []).filter(r => r.text.trim()) } };
  }
  return { ...ed.meta, bpmn: xml, tasks: ed.tasks, activities, task_types: ed.taskTypes };
}

let lintTimer = null;
function lintSoon() {
  clearTimeout(lintTimer);
  lintTimer = setTimeout(async () => {
    try { ed.lint = await api("/tracks/lint", { method: "POST", body: await payload() }); drawLint(); }
    catch (_) { /* ignore while editing */ }
  }, 800);
}

async function saveTrack() {
  const btn = document.getElementById("saveBtn");
  btn.disabled = true;
  try {
    const body = await payload();
    const view = ed.isNew ? await api("/tracks", { method: "POST", body }) : await api("/tracks/" + ed.meta.id, { method: "PUT", body });
    const box = ed.modeler.get("canvas").viewbox(), selId = ed.sel?.id, wasNew = ed.isNew;
    toast(wasNew ? "Трек создан" : `Сохранено, версия v${view.track.version}`);
    await reopen(view, box, selId);
    if (wasNew) history.replaceState(null, "", "#/tracks/" + view.track.id);
  } catch (e) { toast(e.message, true); btn.disabled = false; }
}

// reload the saved track, keeping zoom and selection (the server may normalize the scheme)
async function reopen(view, box, selId) {
  const modeler = ed.modeler, tab = ed.tab;
  const t = view.track;
  Object.assign(ed, { meta: { id: t.id, name: t.name, description: t.description || "", agent_instructions: t.agent_instructions || "", model: t.model || "" },
    bpmn: t.bpmn, tasks: t.tasks, activities: JSON.parse(JSON.stringify(t.activities)), taskTypes: {}, status: t.status,
    version: t.version, versions: view.versions, lint: view.lint, isNew: false, dirty: false, sel: null });
  await modeler.importXML(t.bpmn);
  if (box) modeler.get("canvas").viewbox(box);
  ed.dirty = false;
  drawHead(); drawLint(); drawOverlays();
  const el = selId && modeler.get("elementRegistry").get(selId);
  if (el) modeler.get("selection").select(el); else drawPanel();
  if (tab !== "scheme") switchTab(tab);
}

async function setStatus(action) {
  if (dirty()) { toast("Сначала сохраните изменения", true); return; }
  try {
    const view = await api(`/tracks/${ed.meta.id}/${action}`, { method: "POST" });
    toast({ publish: "Опубликован", unpublish: "Снят с публикации", archive: "Отправлен в архив" }[action]);
    await reopen(view, ed.modeler.get("canvas").viewbox(), ed.sel?.id);
  } catch (e) { toast(e.message, true); }
}

async function deleteTrack() {
  if (!await confirmBox("Удалить трек?", `Трек «${ed.meta.name}» и все его версии будут удалены безвозвратно.`, "Удалить")) return;
  try { await api("/tracks/" + ed.meta.id, { method: "DELETE" }); ed.dirty = false; location.hash = "#/tracks"; }
  catch (e) { toast(e.message, true); }
}

async function loadVersion(v) {
  if (dirty() && !await confirmBox("Несохранённые изменения", "Открыть другую версию и потерять изменения?", "Открыть")) { drawHead(); return; }
  const view = await api(`/tracks/${ed.meta.id}?version=${v}`);
  view.track.status = ed.status;
  const latest = ed.versions.at(-1);
  await reopen(view, null, null);
  fitReadable(ed.modeler);
  if (v !== latest) { ed.dirty = true; drawHead(); toast(`Открыта версия v${v}. Сохранение сделает её новой текущей версией.`); }
}

function exportMenu() {
  if (dirty()) toast("В архив попадёт последняя сохранённая версия");
  const token = store.get("analyst_token");
  const base = `${API}/tracks/${ed.meta.id}/export?version=${ed.version}` + (token ? "&token=" + encodeURIComponent(token) : "");
  modal({
    title: "Экспорт трека",
    body: h("div.stack",
      h("p.muted", "Архив в формате партнёрской системы: scheme.bpmn, tasks.json и папки Activity_*."),
      h("div.stack",
        h("a.btn", { href: base + "&pure=true", download: "" }, "Для партнёрской системы — только их файлы"),
        h("a.btn", { href: base, download: "" }, "Полный — с настройками агента (для этой платформы)"))),
    actions: [{ label: "Закрыть" }],
  });
}
