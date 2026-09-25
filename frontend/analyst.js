// Analyst app: overview, track editor, per-track stats, runs, settings.
"use strict";

const app = document.getElementById("app");
let timer = null;        // auto-refresh of live pages; the track editor lives in editor.js

async function load(fn) {
  try { return await fn(); }
  catch (e) {
    if (e.status === 401) { askToken(); throw e; }
    mount(app, h("div.card.empty", "Ошибка: " + e.message));
    throw e;
  }
}

function askToken() {
  const input = h("input", { type: "password", placeholder: "ANALYST_TOKEN" });
  modal({
    title: "Нужен токен аналитика",
    body: h("div.stack", h("p.muted", "Сервер защищён токеном. Он сохранится только в этом браузере."), input),
    actions: [{ label: "Войти", primary: true, onclick: () => { store.set("analyst_token", input.value.trim()); route(); } }],
  });
}

function pageHead(title, ...right) {
  return h("div.page-head", typeof title === "string" ? h("h1", title) : title, h("div.row.wrap", right));
}

function hbar(value, max, label, cls = "") {
  const pct = max ? Math.max(2, Math.round(100 * value / max)) : 0;
  return h("div.hbar", h("div.track", h("div.fill" + cls, { style: { width: (value ? pct : 0) + "%" } })), h("span", label));
}

// ---------------------------------------------------------------------------
// Overview
// ---------------------------------------------------------------------------

async function renderOverview() {
  const ov = await load(() => api("/stats/overview"));
  const slow = ov.per_track.flatMap(t => t.steps.filter(s => s.avg_s !== null)
    .map(s => ({ ...s, track: t.name, track_id: t.track_id }))).sort((a, b) => b.avg_s - a.avg_s).slice(0, 6);
  const slowMax = slow[0]?.avg_s || 0;
  mount(app,
    pageHead("Обзор", h("span.muted.small", "обновляется автоматически")),
    h("div.kpis",
      kpi("Сейчас проходят", ov.active, ov.stalled ? `${ov.stalled} застряли` : "никто не застрял"),
      kpi("Завершено", ov.completed, `из ${ov.runs} запусков`),
      kpi("Среднее время трека", fmtDuration(ov.avg_duration_s), "по завершённым"),
      kpi("Задач в Jira", ov.jira_issues, "создано агентом"),
      kpi("Треков", ov.published, `опубликовано из ${ov.tracks}`)),
    h("div.card", { style: { marginBottom: "16px" } },
      h("div.pad", { style: { padding: "14px 16px 4px" } }, h("h2", "Кто где сейчас")),
      ov.in_progress.length ? h("div.table-wrap", runsTable(ov.in_progress)) : h("div.empty", "Сейчас никто не проходит треки")),
    h("div.two-col",
      h("div.card.pad", h("h2.section-title", "Треки"),
        ov.per_track.length ? h("table",
          h("tr", h("th", "Трек"), h("th.num", "Запусков"), h("th.num", "Сейчас"), h("th", "Доходят до конца")),
          ov.per_track.map(t => h("tr.click", { onclick: () => location.hash = "#/stats/" + t.track_id },
            h("td", t.name, " ", badge(t.status)), h("td.num", t.runs), h("td.num", t.active),
            h("td.bar-cell", t.runs ? hbar(t.completion_rate, 100, t.completion_rate + "%") : h("span.muted", "—")))))
          : h("div.empty", "Треков пока нет")),
      h("div.card.pad", h("h2.section-title", "Самые долгие шаги"),
        slow.length ? h("table", slow.map(s => h("tr.click", { onclick: () => location.hash = "#/stats/" + s.track_id },
          h("td", h("div", s.title), h("div.small.muted", s.track)),
          h("td.bar-cell", hbar(s.avg_s, slowMax, fmtDuration(s.avg_s), ".warn")))))
          : h("div.empty", "Данные появятся после первых прохождений"))));
  timer = setTimeout(() => location.hash.replace("#", "") in { "": 1, "/": 1 } && renderOverview().catch(() => {}), 15000);
}

function kpi(label, value, sub) {
  return h("div.card.kpi", h("div.label", label), h("div.value", value ?? "—"), h("div.sub", sub || ""));
}

function runsTable(rows, showTrack = true) {
  return h("table",
    h("tr", h("th", "Пользователь"), showTrack ? h("th", "Трек") : null, h("th", "Шаг"), h("th.num", "На шаге"),
      h("th", "Прогресс"), h("th", "Статус"), h("th", "Активность")),
    rows.map(r => h("tr.click", { onclick: () => location.hash = "#/runs/" + r.run_id },
      h("td", h("strong", r.user_name)),
      showTrack ? h("td", r.track_name, h("span.muted.small", ` v${r.track_version}`)) : null,
      h("td", r.current_step_title || "—", r.current_group ? h("div.small.muted", r.current_group) : null),
      h("td.num", fmtDuration(r.on_step_s)),
      h("td.bar-cell", hbar(r.progress, 100, r.progress + "%")),
      h("td", badge(r.status)),
      h("td.muted.small", fmtWhen(r.updated_at)))));
}

// ---------------------------------------------------------------------------
// Tracks list
// ---------------------------------------------------------------------------

async function renderTracks() {
  const tracks = await load(() => api("/tracks"));
  mount(app,
    pageHead("Треки",
      h("button", { onclick: importTrack }, "Импорт"),
      h("button", { onclick: draftFromText }, "✨ Из описания"),
      h("button.primary", { onclick: newTrack }, "+ Новый трек")),
    tracks.length ? h("div.card.table-wrap", h("table",
      h("tr", h("th", "Название"), h("th", "Статус"), h("th.num", "Версия"), h("th.num", "Шагов"),
        h("th.num", "Запусков"), h("th.num", "Сейчас"), h("th", "Изменён"), h("th", "")),
      tracks.map(t => h("tr.click", { onclick: () => location.hash = "#/tracks/" + t.id },
        h("td", h("strong", t.name), h("div.small.muted", t.description || t.id)),
        h("td", badge(t.status)), h("td.num", "v" + t.version), h("td.num", t.steps),
        h("td.num", t.runs), h("td.num", t.active), h("td.muted.small", fmtWhen(t.updated_at)),
        h("td", h("button.sm", { onclick: e => { e.stopPropagation(); location.hash = "#/stats/" + t.id; } }, "Статистика"))))))
      : h("div.card.empty", h("p", "Треков пока нет."), h("button.primary", { onclick: newTrack }, "Создать первый трек")));
}

function newTrack() {
  const name = h("input", { placeholder: "Например: Установка релиза" });
  const id = h("input", { placeholder: "release-installation" });
  let touched = false;
  name.addEventListener("input", () => { if (!touched) id.value = slugify(name.value); });
  id.addEventListener("input", () => { touched = true; });
  modal({
    title: "Новый трек",
    body: h("div.stack",
      h("label.field", h("span", "Название"), name),
      h("label.field", h("span", "Идентификатор"), id, h("div.hint", "Латиница, цифры, - и _. Потом не меняется."))),
    actions: [{ label: "Отмена" }, { label: "Создать", primary: true, onclick: async () => {
      if (!name.value.trim() || !/^[a-zA-Z0-9_-]+$/.test(id.value)) { toast("Заполните название и корректный идентификатор", true); return false; }
      try {
        const view = await api("/tracks", { method: "POST", body: { id: id.value, name: name.value.trim() } });
        location.hash = "#/tracks/" + view.track.id;
      } catch (e) { toast(e.message, true); return false; }
    } }],
  });
}

function draftFromText() {
  const text = h("textarea", { rows: 8, placeholder: "Например: разработчик хочет поставить релиз. Нужно узнать команду, версию и окружение, проверить что тесты зелёные, создать заявку в Jira. Для прода нужно согласование релиз-менеджера…" });
  modal({
    title: "Черновик трека из описания",
    body: h("div.stack", h("p.muted", "Опишите процесс своими словами. Агент нарисует схему: шаги, свойства, развилки и шаги с отправкой в Jira. Дальше доработаете в редакторе."), text),
    actions: [{ label: "Отмена" }, { label: "Сгенерировать", primary: true, onclick: async () => {
      if (!text.value.trim()) return false;
      toast("Агент составляет трек…");
      try { openEditor(await api("/tracks/draft", { method: "POST", body: { description: text.value } }), true); }
      catch (e) { toast(e.message, true); return false; }
    } }],
  });
}

function importTrack() {
  const file = h("input", { type: "file", accept: ".zip,application/zip" });
  const id = h("input", { placeholder: "из архива" });
  const name = h("input", { placeholder: "из схемы" });
  const send = async (replace = false) => {
    const f = file.files[0];
    if (!f) { toast("Выберите zip-архив", true); return false; }
    const q = new URLSearchParams();
    if (id.value.trim()) q.set("id", id.value.trim());
    if (name.value.trim()) q.set("name", name.value.trim());
    if (replace) q.set("replace", "true");
    const headers = { "Content-Type": "application/zip" };
    const token = store.get("analyst_token");
    if (token) headers["X-Analyst-Token"] = token;
    const res = await fetch(`${API}/tracks/import?${q}`, { method: "POST", headers, body: f });
    const data = await res.json().catch(() => ({}));
    if (res.status === 409 && !replace) {
      if (await confirmBox("Трек уже есть", data.detail + ". Заменить его новой версией из архива?", "Заменить")) return send(true);
      return false;
    }
    if (!res.ok) { toast(data.detail || `Ошибка ${res.status}`, true); return false; }
    toast("Трек импортирован" + (data.lint?.errors?.length ? `, ошибок: ${data.lint.errors.length}` : ""));
    location.hash = "#/tracks/" + data.track.id;
  };
  modal({
    title: "Импорт трека",
    body: h("div.stack",
      h("p.muted", "Zip-архив из партнёрской системы: scheme.bpmn, tasks.json и папки Activity_*. Файлы сохраняются как есть; для шагов без папки создаются пустые описания."),
      file,
      h("div.grid-2",
        h("label.field", h("span", "Идентификатор"), id),
        h("label.field", h("span", "Название"), name))),
    actions: [{ label: "Отмена" }, { label: "Импортировать", primary: true, onclick: () => send() }],
  });
}

// ---------------------------------------------------------------------------
// Track stats
// ---------------------------------------------------------------------------

async function renderStats(id) {
  const [st, runs] = await load(() => Promise.all([api("/stats/tracks/" + id), api("/admin/runs?track_id=" + id)]));
  const maxAvg = Math.max(0, ...st.steps.map(s => s.avg_s || 0));
  mount(app,
    pageHead(h("div.row.wrap", h("a", { href: "#/tracks" }, "Треки"), h("span.muted", "/"), h("h1", st.name), badge(st.status)),
      h("button", { onclick: () => location.hash = "#/tracks/" + id }, "Редактировать")),
    h("div.kpis",
      kpi("Запусков", st.runs, `${st.cancelled} отменено`),
      kpi("Сейчас проходят", st.active),
      kpi("Завершили", st.completed, st.completion_rate !== null ? `${st.completion_rate}% запусков` : ""),
      kpi("Среднее время", fmtDuration(st.duration.avg_s), "медиана " + fmtDuration(st.duration.median_s)),
      kpi("Самое долгое", fmtDuration(st.duration.max_s))),
    h("div.card", { style: { marginBottom: "16px" } },
      h("div", { style: { padding: "14px 16px 4px" } }, h("h2", "Шаги"),
        h("div.small.muted", "В порядке схемы. Дошли: сколько запусков побывало на шаге. Время — от входа на шаг до выхода, ожидание ответа пользователя тоже входит.")),
      h("div.table-wrap", h("table",
        h("tr", h("th", "#"), h("th", "Шаг"), h("th", "Дошли"), h("th", "Сейчас на шаге"), h("th", "Среднее время"), h("th.num", "Медиана"), h("th.num", "Максимум")),
        st.steps.map((s, i) => h("tr",
          h("td.muted", i + 1), h("td", h("strong", s.title), s.group ? h("div.small.muted", s.group) : null),
          h("td.bar-cell", hbar(s.reached, st.runs, `${s.reached}`)),
          h("td", s.active_now ? h("div", h("span.badge.active", s.active_now), " ", h("span.users-inline", s.users_now.join(", "))) : h("span.muted", "—")),
          h("td.bar-cell", s.avg_s !== null ? hbar(s.avg_s, maxAvg, fmtDuration(s.avg_s), ".warn") : h("span.muted", "—")),
          h("td.num", fmtDuration(s.median_s)), h("td.num", fmtDuration(s.max_s))))))),
    h("div.card", h("div", { style: { padding: "14px 16px 4px" } }, h("h2", "Прохождения")),
      runs.length ? h("div.table-wrap", runsTable(runs, false)) : h("div.empty", "Пока никто не проходил этот трек")));
}

// ---------------------------------------------------------------------------
// Runs
// ---------------------------------------------------------------------------

async function renderRuns() {
  const q = new URLSearchParams(location.hash.split("?")[1] || "");
  const [tracks, rows] = await load(() => Promise.all([api("/tracks"),
    api("/admin/runs?" + new URLSearchParams([...q].filter(([, v]) => v)))]));
  const trackSel = h("select", { style: { width: "auto" } }, h("option", { value: "" }, "Все треки"),
    tracks.map(t => h("option", { value: t.id, selected: q.get("track_id") === t.id }, t.name)));
  const statusSel = h("select", { style: { width: "auto" } }, h("option", { value: "" }, "Любой статус"),
    ["active", "stalled", "completed", "cancelled"].map(s => h("option", { value: s, selected: q.get("status") === s }, STATUS_RU[s])));
  const apply = () => { location.hash = "#/runs?" + new URLSearchParams({ track_id: trackSel.value, status: statusSel.value }); };
  trackSel.onchange = statusSel.onchange = apply;
  mount(app, pageHead("Прохождения", trackSel, statusSel),
    rows.length ? h("div.card.table-wrap", runsTable(rows)) : h("div.card.empty", "Ничего не найдено"));
  timer = setTimeout(() => location.hash.startsWith("#/runs") && !location.hash.match(/^#\/runs\/[a-f0-9]/) && renderRuns().catch(() => {}), 15000);
}

async function renderRun(id) {
  const v = await load(() => api("/admin/runs/" + id));
  const { run, row } = v;
  const titles = Object.fromEntries(v.progress.steps.map(s => [s.id, s.title]));
  const msgs = run.messages.map(m => m.role === "event" ? h("div.msg.event", m.text)
    : h("div.msg." + m.role, m.role === "assistant" ? h("div.md", { html: md(m.text) }) : h("div", m.text), h("time", fmtWhen(m.at))));
  mount(app,
    pageHead(h("div.row.wrap", h("a", { href: "#/runs" }, "Прохождения"), h("span.muted", "/"), h("h1", row.user_name), badge(row.status)),
      h("button", { onclick: () => renderRun(id) }, "Обновить")),
    h("div.two-col", { style: { alignItems: "start" } },
      h("div.stack",
        h("div.card.pad", h("h2.section-title", "Сводка"), h("dl.kv",
          h("dt", "Трек"), h("dd", h("a", { href: "#/stats/" + run.track_id }, run.track_name), ` v${run.track_version}`),
          h("dt", "Шаг"), h("dd", row.current_step_title || "—", row.on_step_s !== null ? h("span.muted", ` · ${fmtDuration(row.on_step_s)}`) : null),
          h("dt", "Прогресс"), h("dd", hbar(row.progress, 100, row.progress + "%")),
          h("dt", "Начато"), h("dd", fmtWhen(run.created_at)),
          h("dt", "Активность"), h("dd", fmtWhen(run.updated_at)),
          h("dt", "Длительность"), h("dd", fmtDuration(row.duration_s)),
          run.summary ? [h("dt", "Итог"), h("dd", run.summary)] : null)),
        h("div.card.pad", h("h2.section-title", "Путь по шагам"), h("ul.timeline",
          run.visits.map(vi => h("li", h("span", titles[vi.step_id] || vi.step_id, h("div.small.muted", fmtWhen(vi.entered_at))),
            h("span.muted", vi.left_at ? fmtDuration((new Date(vi.left_at) - new Date(vi.entered_at)) / 1000) : "сейчас здесь"))))),
        h("div.card.pad", h("h2.section-title", "Собранные данные"),
          Object.keys(run.facts).length ? h("dl.kv", Object.entries(run.facts).flatMap(([k, val]) =>
            [h("dt", k), h("dd", typeof val === "object" ? JSON.stringify(val) : String(val))])) : h("div.muted", "Пока ничего")),
        Object.keys(run.confirmed).length ? h("div.card.pad", h("h2.section-title", "Подтверждённые условия"),
          h("dl.kv", Object.entries(run.confirmed).flatMap(([sid, reqs]) => Object.entries(reqs).flatMap(([rid, ev]) =>
            [h("dt", `${titles[sid] || sid}`), h("dd", h("strong", rid), " — ", ev)])))) : null,
        run.jira_issues.length ? h("div.card.pad", h("h2.section-title", "Задачи Jira"), h("table",
          run.jira_issues.map(i => h("tr", h("td", i.url ? h("a", { href: i.url, target: "_blank" }, i.key) : h("strong", i.key)),
            h("td", i.summary), h("td.muted", i.status))))) : null),
      h("div.card.pad", h("h2.section-title", "Диалог"), h("div.transcript", msgs))));
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

async function renderSettings() {
  const s = await load(() => api("/settings"));
  const secret = (obj, key) => {
    const el = h("input", { type: "password", autocomplete: "off", placeholder: obj[key + "_set"] ? "задан, введите новый, чтобы заменить" : "не задан" });
    el.addEventListener("input", () => { obj[key] = el.value; });
    obj[key] = "";
    return el;
  };
  const num = (obj, key, attrs = {}) => { const el = h("input", { type: "number", value: obj[key], ...attrs });
    el.addEventListener("input", () => { obj[key] = Number(el.value); }); return el; };
  const text = (obj, key, attrs = {}) => { const el = h(attrs.textarea ? "textarea" : "input", { ...attrs, textarea: null, value: obj[key] || "" });
    el.addEventListener("input", () => { obj[key] = el.value; }); return el; };
  const choose = (obj, key, opts) => { const el = h("select", Object.entries(opts).map(([v, l]) => h("option", { value: v, selected: obj[key] === v }, l)));
    el.addEventListener("change", () => { obj[key] = el.value; jiraFields(); }); return el; };
  const testBtn = (path, label) => {
    const out = h("span.small");
    return h("div.row", h("button", { onclick: async () => {
      out.textContent = "проверяю…"; out.className = "small muted";
      try { await save(false); const r = await api(path, { method: "POST" }); out.textContent = (r.ok ? "✓ " : "✕ ") + r.message; out.className = "small " + (r.ok ? "" : "muted"); out.style.color = r.ok ? "var(--ok)" : "var(--bad)"; }
      catch (e) { out.textContent = "✕ " + e.message; out.style.color = "var(--bad)"; }
    } }, label), out);
  };
  const jsonField = (obj, key) => {
    const el = h("textarea", { rows: 2, style: { fontFamily: "var(--mono)", fontSize: "12.5px" },
      value: Object.keys(obj[key] || {}).length ? JSON.stringify(obj[key]) : "", placeholder: "{}" });
    el.addEventListener("change", () => {
      try { obj[key] = el.value.trim() ? JSON.parse(el.value) : {}; el.style.borderColor = ""; }
      catch (_) { el.style.borderColor = "var(--bad)"; toast("Некорректный JSON", true); }
    });
    return el;
  };
  const jiraCloud = h("div.stack");
  function jiraFields() {
    const m = s.jira.mode;
    mount(jiraCloud, m === "mock" ? h("p.small.muted", "Тестовый режим: задачи создаются локально с номерами вида REL-1. Удобно, чтобы отладить трек до подключения Jira.") :
      h("div.stack",
        h("label.field", h("span", "Адрес Jira"), text(s.jira, "base_url", { placeholder: "https://company.atlassian.net" })),
        m === "cloud" ? h("label.field", h("span", "Email"), text(s.jira, "email")) : null,
        h("label.field", h("span", m === "cloud" ? "API-токен" : "Personal access token"), secret(s.jira, "token"))));
  }
  jiraFields();
  const tokenInput = h("input", { type: "password", value: store.get("analyst_token") || "", placeholder: "пусто, если сервер без токена" });

  async function save(show = true) {
    await api("/settings", { method: "PUT", body: s });
    store.set("analyst_token", tokenInput.value.trim());
    if (show) { toast("Настройки сохранены"); renderSettings(); }
  }

  mount(app,
    pageHead("Настройки", h("button.primary", { onclick: () => save().catch(e => toast(e.message, true)) }, "Сохранить")),
    h("div.settings-grid",
      h("div.card.pad.stack", h("h2", "Агент"),
        h("label.field", h("span", "URL API (совместимый с OpenAI)"), text(s.agent, "base_url", { placeholder: "https://api.mistral.ai/v1" }),
          h("div.hint", "Mistral, OpenAI, vLLM, Ollama: любой сервис с /chat/completions и tool calling.")),
        h("div.grid-2",
          h("label.field", h("span", "Модель"), text(s.agent, "model")),
          h("label.field", h("span", "API-ключ"), secret(s.agent, "api_key"))),
        h("div.grid-3",
          h("label.field", h("span", "Температура"), num(s.agent, "temperature", { step: 0.1, min: 0, max: 2 })),
          h("label.field", h("span", "Макс. токенов ответа"), num(s.agent, "max_tokens", { min: 64 })),
          h("label.field", h("span", "Сообщений в памяти"), num(s.agent, "history_limit", { min: 4 }))),
        h("label.field", h("span", "Дополнительные инструкции для всех треков"),
          text(s.agent, "system_prompt", { textarea: true, rows: 4, placeholder: "Например: обращайся на «вы», не используй эмодзи" })),
        h("label.field", h("span", "Доп. параметры запроса (JSON)"), jsonField(s.agent, "extra_body"),
          h("div.hint", "Добавляются к каждому запросу. Например, для GLM (z.ai) {\"thinking\": {\"type\": \"disabled\"}} ускоряет ответы.")),
        testBtn("/settings/test-agent", "Проверить агента")),
      h("div.card.pad.stack", h("h2", "Jira"),
        h("div.grid-2",
          h("label.field", h("span", "Режим"), choose(s.jira, "mode", { mock: "Тестовый (без Jira)", cloud: "Jira Cloud", server: "Jira Server / Data Center" })),
          h("label.field", h("span", "Проект по умолчанию"), text(s.jira, "default_project", { placeholder: "REL" }))),
        jiraCloud,
        testBtn("/settings/test-jira", "Проверить подключение")),
      h("div.card.pad.stack", h("h2", "Материалы и Confluence"),
        h("p.small.muted", "Агент читает ссылки из шагов. Для закрытых страниц Confluence укажите доступ: адрес, email и токен для Cloud или только токен (PAT) для Server."),
        h("label.field", h("span", "Адрес Confluence"), text(s.links, "confluence_base_url", { placeholder: "https://company.atlassian.net/wiki" })),
        h("div.grid-2",
          h("label.field", h("span", "Email (Cloud)"), text(s.links, "confluence_email")),
          h("label.field", h("span", "Токен"), secret(s.links, "confluence_token"))),
        h("div.grid-2",
          h("label.field", h("span", "Таймаут, с"), num(s.links, "timeout_s", { min: 2 })),
          h("label.field", h("span", "Символов из ссылки в контекст"), num(s.links, "max_chars", { min: 1000, step: 1000 })))),
      h("div.card.pad.stack", h("h2", "Прохождения и доступ"),
        h("label.field", h("span", "Считать «застрявшим» без активности, часов"), num(s.runs, "stale_hours", { min: 1 })),
        h("label.field", h("span", "Токен аналитика в этом браузере"), tokenInput,
          h("div.hint", "Нужен, если сервер запущен с переменной ANALYST_TOKEN.")))));
}

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

let lastHash = location.hash;
function route() {
  clearTimeout(timer);
  const hash = location.hash || "#/";
  const path = hash.slice(1).split("?")[0];
  const section = path.startsWith("/tracks") ? "tracks" : path.startsWith("/runs") ? "runs"
    : path.startsWith("/settings") ? "settings" : path.startsWith("/stats") ? "tracks" : "overview";
  document.querySelectorAll(".nav a").forEach(a => a.classList.toggle("active", a.dataset.nav === section));
  let m;
  const done = p => p?.catch?.(() => {});
  if (path === "/tracks/new") { if (!ed?.isNew) { location.replace("#/tracks"); return; } }
  else if ((m = path.match(/^\/tracks\/([\w-]+)$/))) done(renderEditor(m[1]));
  else if (path === "/tracks") done(renderTracks());
  else if ((m = path.match(/^\/stats\/([\w-]+)$/))) done(renderStats(m[1]));
  else if ((m = path.match(/^\/runs\/([\w-]+)$/))) done(renderRun(m[1]));
  else if (path === "/runs") done(renderRuns());
  else if (path === "/settings") done(renderSettings());
  else done(renderOverview());
  if (!path.startsWith("/tracks/") && ed) { ed.modeler?.destroy(); ed = null; }
  lastHash = location.hash;
}

window.addEventListener("hashchange", () => {
  if (dirty() && !confirm("Есть несохранённые изменения трека. Уйти без сохранения?")) {
    history.replaceState(null, "", lastHash); return;
  }
  route();
});
window.addEventListener("beforeunload", e => { if (dirty()) { e.preventDefault(); e.returnValue = ""; } });
route();
