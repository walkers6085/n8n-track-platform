// User app: pick a track, go through it with the agent, come back any time.
"use strict";

const app = document.getElementById("app");
const me = {
  get id() {
    let id = store.get("user_id");
    if (!id) { id = randomId(); store.set("user_id", id); }
    return id;
  },
  get name() { return store.get("user_name") || ""; },
};

function askName(force = false) {
  return new Promise(resolve => {
    if (me.name && !force) return resolve(me.name);
    const input = h("input", { value: me.name, placeholder: "Например, Мария Иванова", maxlength: 80 });
    const save = () => {
      const v = input.value.trim();
      if (!v) { input.focus(); return false; }
      store.set("user_name", v); renderWho(); resolve(v);
    };
    input.addEventListener("keydown", e => { if (e.key === "Enter") { save() !== false && close(); } });
    const close = modal({
      title: "Как к вам обращаться?",
      body: h("div.stack", h("p.muted", "Агент будет обращаться к вам по имени, а аналитик увидит, кто проходит трек."),
        input),
      actions: [{ label: "Продолжить", primary: true, onclick: save }],
      onclose: () => resolve(me.name),
    });
  });
}

function renderWho() {
  const b = document.getElementById("who");
  const initials = me.name.split(/\s+/).map(w => w[0]).join("").slice(0, 2).toUpperCase();
  mount(b, me.name ? [h("span.avatar", initials), me.name] : ["Представиться"]);
  b.onclick = () => askName(true);
}

// ---------------------------------------------------------------------------
// Home: continue / start
// ---------------------------------------------------------------------------

async function renderHome() {
  mount(app, h("div.page", h("div.empty", "Загрузка…")));
  let catalog, runs;
  try {
    [catalog, runs] = await Promise.all([api("/catalog"), api("/runs?user_id=" + me.id)]);
  } catch (e) { mount(app, h("div.page", h("div.empty", "Не удалось загрузить: " + e.message))); return; }
  const active = runs.filter(r => r.status === "active" || r.status === "stalled");
  const finished = runs.filter(r => !active.includes(r));

  const runCard = r => h("a.card.track-card", { href: "#/run/" + r.run_id, style: { color: "inherit", textDecoration: "none" } },
    h("div.row", h("h3", r.track_name), h("span.spacer"), badge(r.status === "stalled" ? "active" : r.status)),
    h("p", r.current_step_title ? "Сейчас: " + r.current_step_title : ""),
    h("div.progress", h("i", { style: { width: r.progress + "%" } })),
    h("div.meta", h("span", `${r.progress}%`), h("span.spacer"), h("span", "обновлено " + fmtWhen(r.updated_at))));

  const startCard = t => h("div.card.track-card",
    h("h3", t.name), h("p", t.description || "Без описания"),
    h("div.meta", h("span", `${t.steps} ${plural(t.steps, "шаг", "шага", "шагов")}`), h("span.spacer"),
      h("button.primary", { onclick: e => startRun(t, e.currentTarget) }, "Начать")));

  mount(app, h("div.page",
    h("header", h("h1", me.name ? `Здравствуйте, ${me.name.split(" ")[0]}!` : "Здравствуйте!"),
      h("p.muted", "Агент проведёт вас по треку шаг за шагом. Прервать прохождение и вернуться к нему можно в любой момент.")),
    active.length ? h("section.stack", { style: { marginBottom: "28px" } },
      h("h2", "Продолжить"), h("div.grid-cards", active.map(runCard))) : null,
    h("section.stack", h("h2", "Начать трек"),
      catalog.length ? h("div.grid-cards", catalog.map(startCard))
        : h("div.card.empty", "Пока нет опубликованных треков.")),
    finished.length ? h("section.stack", { style: { marginTop: "28px" } },
      h("h2", "История"),
      h("div.card.table-wrap", h("table",
        h("tr", h("th", "Трек"), h("th", "Статус"), h("th", "Задачи"), h("th", "Когда")),
        finished.map(r => h("tr.click", { onclick: () => location.hash = "#/run/" + r.run_id },
          h("td", r.track_name), h("td", badge(r.status)), h("td", r.jira.join(", ") || "—"),
          h("td.muted", fmtWhen(r.finished_at || r.updated_at))))))) : null));
}

function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20)) return few;
  return many;
}

async function startRun(track, btn) {
  const name = await askName();
  if (!name) return;
  btn.disabled = true; btn.textContent = "Запускаем…";
  try {
    const v = await api("/runs", { method: "POST", body: { track_id: track.id, user_id: me.id, user_name: name } });
    cache = v;
    location.hash = "#/run/" + v.run.run_id;
  } catch (e) { toast(e.message, true); btn.disabled = false; btn.textContent = "Начать"; }
}

// ---------------------------------------------------------------------------
// Run: steps | chat | current step details
// ---------------------------------------------------------------------------

let cache = null;   // last run view, avoids a refetch right after start
let sending = false;

async function renderRun(runId) {
  let view = cache && cache.run.run_id === runId ? cache : null;
  cache = null;
  if (!view) {
    mount(app, h("div.page", h("div.empty", "Загрузка…")));
    try { view = await api("/runs/" + runId); }
    catch (e) { mount(app, h("div.page", h("div.empty", e.message, h("p", h("a", { href: "#/" }, "На главную"))))); return; }
  }
  const side = h("aside.run-side");
  const log = h("div.chat-log");
  const input = h("textarea", { rows: 1, placeholder: "Ваш ответ…", title: "Enter — отправить, Shift+Enter — новая строка" });
  const sendBtn = h("button.primary", { title: "Отправить" }, "Отправить");
  const chips = h("div.chips");
  const composer = h("div.composer", chips, h("div.row", input, sendBtn));
  const banner = h("div.done-banner.hidden");
  const stepsCol = h("nav.run-steps");
  const headTitle = h("div", { style: { minWidth: 0 } });

  const head = h("div.chat-head",
    h("a.btn.ghost", { href: "#/", title: "Все треки" }, "←"), headTitle, h("span.spacer"),
    h("button.sm.mobile-only", { onclick: () => side.classList.toggle("open") }, "Детали"),
    h("button.ghost.sm.danger", { onclick: () => cancelRun(runId), id: "cancelBtn", title: "Отменить прохождение" }, "Отменить"));

  mount(app, h("div.run-layout", stepsCol, h("section.chat", head, log, banner, composer), side));

  const autosize = () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 180) + "px"; };
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  sendBtn.onclick = () => send();

  async function send(text) {
    text = (text ?? input.value).trim();
    if (!text || sending) return;
    sending = true; sendBtn.disabled = true; input.value = ""; autosize();
    view.run.messages.push({ role: "user", text, at: new Date().toISOString() });
    drawLog(true);
    try {
      view = await api(`/runs/${runId}/messages`, { method: "POST", body: { text } });
    } catch (e) {
      toast(e.message, true);
      view.run.messages.pop(); input.value = text;
    }
    sending = false; sendBtn.disabled = false;
    draw(); input.focus();
  }

  function drawLog(typing = false) {
    const msgs = view.run.messages.map(m => {
      if (m.role === "event") return h("div.msg.event" + (m.text.startsWith("⚠") ? ".warn" : ""), m.text);
      const body = m.role === "assistant" ? h("div.md", { html: md(m.text) }) : h("div", m.text);
      return h("div.msg." + m.role, body, h("time", fmtTime(m.at)));
    });
    if (typing) msgs.push(h("div.msg.assistant", h("span.typing", h("i"), h("i"), h("i"))));
    mount(log, msgs);
    log.scrollTop = log.scrollHeight;
  }

  function draw() {
    const { run, progress } = view;
    const cur = progress.current;
    const active = run.status === "active";
    const shown = v => v === true ? "да" : v === false ? "нет" : typeof v === "object" ? JSON.stringify(v) : String(v);
    mount(headTitle, h("h2", { style: { whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" } }, run.track_name),
      h("div.small.muted", { style: { whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" } },
        active && cur ? [cur.group, cur.title].filter(Boolean).join(" · ") : STATUS_RU[run.status]));
    document.getElementById("cancelBtn").classList.toggle("hidden", !active);

    // steps column, grouped by the scheme's stages
    const items = [];
    let lastGroup;
    progress.steps.forEach((s, i) => {
      if (s.group !== lastGroup && s.group) items.push(h("li.group-title", s.group));
      lastGroup = s.group;
      const state = run.status === "completed" && s.state !== "todo" ? "done" : s.state;
      items.push(h("li." + state, h("span.dot", state === "done" ? "✓" : i + 1), h("span", s.title)));
    });
    mount(stepsCol,
      h("div.row", h("h3", "Шаги"), h("span.spacer"), h("span.small.muted", progress.percent + "%")),
      h("div.progress", { style: { marginTop: "10px" } }, h("i", { style: { width: progress.percent + "%" } })),
      h("ol.steps-list", items),
      h("p.hint", "Путь зависит от ваших ответов: часть шагов может не понадобиться."));

    // side: current step details
    const blocks = [];
    if (cur) {
      blocks.push(h("div.side-block", h("h3", cur.group ? "Этап «" + cur.group + "»" : "Текущий шаг"), h("h2", cur.title),
        cur.jira ? h("span.badge.warn", { style: { marginTop: "6px" } }, "будет создана задача Jira") : null,
        cur.content ? h("div.content.small", { style: { marginTop: "8px" }, html: safeHtml(cur.content) }) : null));
      if (cur.links.length) blocks.push(h("div.side-block", h("h3", "Материалы"),
        cur.links.map(l => h("div.link-item", h("span.muted", "↗"),
          h("a", { href: l.url, target: "_blank", rel: "noopener" }, l.title || l.url)))));
      if (cur.properties.length) blocks.push(h("div.side-block", h("h3", "Нужные данные"), h("ul.checklist",
        cur.properties.map(p => {
          const has = p.value !== null && p.value !== undefined && p.value !== "";
          const label = p.variants.find(v => String(v.value) === String(p.value))?.label;
          return h("li" + (has ? ".ok" : ""), h("span.mark", has ? "✓" : "○"),
            h("span", p.description || p.name || p.code, p.required ? "" : " (необяз.)",
              has ? h("div.val", label || shown(p.value)) : null));
        }))));
      if (cur.requirements.length) blocks.push(h("div.side-block", h("h3", "Обязательные условия"), h("ul.checklist",
        cur.requirements.map(r => h("li" + (r.done ? ".ok" : ""), h("span.mark", r.done ? "✓" : "○"), h("span", r.text))))));
    }
    if (run.jira_issues.length) blocks.push(h("div.side-block", h("h3", "Задачи Jira"),
      run.jira_issues.map(i => h("div.jira-item",
        i.url ? h("a", { href: i.url, target: "_blank", rel: "noopener" }, h("strong", i.key)) : h("strong", i.key),
        h("span.muted.small", { style: { flex: 1 } }, i.status || ""),
        active ? h("button.sm", { onclick: () => send(`Какой статус у задачи ${i.key}?`) }, "Статус") : null))));
    const facts = Object.entries(run.facts);
    if (facts.length) blocks.push(h("div.side-block", h("h3", "Что агент знает"),
      h("dl.kv", facts.flatMap(([k, v]) => [h("dt", k), h("dd", shown(v))]))));
    mount(side, blocks.length ? blocks : h("div.muted.small", "Здесь появятся детали шага."));

    // chat
    drawLog();
    banner.classList.toggle("hidden", active);
    banner.textContent = run.status === "completed" ? "✓ Трек пройден" + (run.summary ? ": " + run.summary : "")
      : "Прохождение отменено";
    composer.classList.toggle("hidden", !active);
    const suggestions = [];
    if (active && Date.now() - new Date(run.updated_at).getTime() > 3600e3) suggestions.push("Напомни, где я остановился");
    if (cur && progress.missing.length === 0 && active) suggestions.push("Готово, идём дальше");
    if (run.messages.at(-1)?.role === "event" && run.messages.at(-1).text.startsWith("⚠")) suggestions.push("Повтори, пожалуйста");
    mount(chips, suggestions.map(s => h("span.chip", { onclick: () => send(s) }, s)));
  }

  draw();
  if (view.run.status === "active") input.focus();
}

async function cancelRun(runId) {
  if (!await confirmBox("Отменить прохождение?", "Прохождение будет закрыто. Трек можно будет начать заново.", "Отменить прохождение")) return;
  try { await api(`/runs/${runId}/cancel`, { method: "POST" }); renderRun(runId); }
  catch (e) { toast(e.message, true); }
}

// ---------------------------------------------------------------------------

function route() {
  const m = location.hash.match(/^#\/run\/([a-zA-Z0-9_-]+)/);
  if (m) renderRun(m[1]); else renderHome();
}
window.addEventListener("hashchange", route);
renderWho();
route();
