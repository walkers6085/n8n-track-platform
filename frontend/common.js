// Shared helpers for both apps: API calls, DOM building, safe markdown, formatting.
"use strict";

const API = "/api";

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  const token = store.get("analyst_token");
  if (token) headers["X-Analyst-Token"] = token;
  const res = await fetch(API + path, {
    method: opts.method || "GET", headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok) {
    const detail = data && data.detail;
    const msg = typeof detail === "string" ? detail
      : Array.isArray(detail) ? detail.map(d => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ")
      : `Ошибка ${res.status}`;
    const err = new Error(msg); err.status = res.status; throw err;
  }
  return data;
}

const store = {
  get(k) { try { return localStorage.getItem(k); } catch (_) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (_) { /* private mode */ } },
};

// h("div.card.pad", {onclick}, child, "text", [children])
function h(tag, attrs, ...children) {
  const [name, ...classes] = tag.split(".");
  const el = document.createElement(name || "div");
  if (classes.length) el.className = classes.join(" ");
  if (attrs && (typeof attrs !== "object" || attrs instanceof Node || Array.isArray(attrs))) {
    children.unshift(attrs); attrs = null;
  }
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className += " " + v;
    else if (k === "html") el.innerHTML = v;
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  const add = c => {
    if (c === null || c === undefined || c === false) return;
    if (Array.isArray(c)) c.forEach(add);
    else el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  };
  children.forEach(add);
  return el;
}

function mount(target, ...children) {
  target.replaceChildren();
  children.flat(Infinity).forEach(c => {
    if (c !== null && c !== undefined && c !== false) target.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  });
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Small, safe markdown: escape first, then a few inline/block rules. Only http(s) links.
function md(src) {
  const inline = s => esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?:;]|$)/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
  const out = [];
  let list = null, para = [], code = null;
  const flushPara = () => { if (para.length) { out.push(`<p>${para.map(inline).join("<br>")}</p>`); para = []; } };
  const flushList = () => { if (list) { out.push(`<${list.tag}>${list.items.map(i => `<li>${inline(i)}</li>`).join("")}</${list.tag}>`); list = null; } };
  for (const line of String(src || "").split("\n")) {
    if (code !== null) {
      if (line.trim().startsWith("```")) { out.push(`<pre>${esc(code.join("\n"))}</pre>`); code = null; }
      else code.push(line);
      continue;
    }
    if (line.trim().startsWith("```")) { flushPara(); flushList(); code = []; continue; }
    const ul = line.match(/^\s*[-*•]\s+(.*)/), ol = line.match(/^\s*\d+[.)]\s+(.*)/);
    const hd = line.match(/^(#{1,4})\s+(.*)/);
    if (ul || ol) {
      flushPara();
      const tag = ul ? "ul" : "ol";
      if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; }
      list.items.push((ul || ol)[1]);
    } else if (hd) { flushPara(); flushList(); out.push(`<h4>${inline(hd[2])}</h4>`); }
    else if (!line.trim()) { flushPara(); flushList(); }
    else { flushList(); para.push(line); }
  }
  if (code !== null) out.push(`<pre>${esc(code.join("\n"))}</pre>`);
  flushPara(); flushList();
  return out.join("");
}

// Step content comes as tiptap HTML. Keep the formatting, drop everything executable.
const SAFE_TAGS = new Set(["P", "BR", "STRONG", "B", "EM", "I", "U", "S", "OL", "UL", "LI", "A", "H1", "H2",
  "H3", "H4", "CODE", "PRE", "BLOCKQUOTE", "SPAN", "MARK", "HR"]);
const DROP_TAGS = new Set(["SCRIPT", "STYLE", "IFRAME", "OBJECT", "EMBED", "TEMPLATE", "SVG", "MATH"]);
function safeHtml(html) {
  const doc = new DOMParser().parseFromString(`<body>${html || ""}</body>`, "text/html");
  const walk = node => {
    for (const c of [...node.childNodes]) {
      if (c.nodeType === 3) continue;
      if (c.nodeType !== 1 || DROP_TAGS.has(c.tagName)) { c.remove(); continue; }
      walk(c);
      if (!SAFE_TAGS.has(c.tagName)) { c.replaceWith(...c.childNodes); continue; }
      const href = c.getAttribute("href"), style = c.getAttribute("style") || "", cls = c.getAttribute("class") || "";
      for (const a of [...c.attributes]) c.removeAttribute(a.name);
      if (c.tagName === "A" && href && /^https?:\/\//i.test(href)) {
        c.setAttribute("href", href); c.setAttribute("target", "_blank"); c.setAttribute("rel", "noopener noreferrer nofollow");
        if (/userlink|user-mention/.test(cls)) c.setAttribute("class", cls.replace(/[^\w\s-]/g, ""));
      }
      const color = style.match(/(?:^|;)\s*color:\s*(#[0-9a-f]{3,8}|rgb\([\d\s,.]+\))/i);
      if (c.tagName === "SPAN" && color) c.setAttribute("style", `color: ${color[1]}`);
    }
  };
  walk(doc.body);
  return doc.body.innerHTML;
}

function fmtDuration(sec) {
  if (sec === null || sec === undefined) return "—";
  sec = Math.round(sec);
  if (sec < 60) return `${sec} с`;
  const m = Math.floor(sec / 60);
  if (m < 60) return `${m} мин`;
  const hrs = Math.floor(m / 60);
  if (hrs < 48) return `${hrs} ч ${m % 60 ? (m % 60) + " мин" : ""}`.trim();
  return `${Math.floor(hrs / 24)} дн ${hrs % 24 ? (hrs % 24) + " ч" : ""}`.trim();
}

function fmtWhen(iso) {
  if (!iso) return "—";
  const d = new Date(iso), diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return "только что";
  if (diff < 3600) return `${Math.floor(diff / 60)} мин назад`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} ч назад`;
  return d.toLocaleDateString("ru-RU", { day: "numeric", month: "short" }) + " " +
    d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}

function fmtTime(iso) {
  return iso ? new Date(iso).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" }) : "";
}

const STATUS_RU = {
  draft: "черновик", published: "опубликован", archived: "в архиве",
  active: "в процессе", completed: "завершён", cancelled: "отменён", stalled: "застрял",
};
const badge = s => h("span.badge." + s, STATUS_RU[s] || s);

function toast(text, bad = false) {
  let box = document.getElementById("toasts");
  if (!box) { box = h("div", { id: "toasts" }); document.body.appendChild(box); }
  const t = h("div.toast" + (bad ? ".bad" : ""), text);
  box.appendChild(t);
  setTimeout(() => t.remove(), bad ? 6000 : 3000);
}

// modal({title, body: Node, actions: [{label, primary, onclick -> false keeps it open}]})
function modal({ title, body, actions = [], onclose }) {
  const close = () => { back.remove(); onclose && onclose(); };
  const back = h("div.modal-back", { onmousedown: e => { if (e.target === back) close(); } },
    h("div.card.modal", h("h2", title), body,
      h("div.actions", actions.map(a => h("button" + (a.primary ? ".primary" : "") + (a.danger ? ".danger" : ""), {
        onclick: async () => { if ((await a.onclick?.()) !== false) close(); },
      }, a.label)))));
  document.body.appendChild(back);
  const first = back.querySelector("input, textarea");
  if (first) setTimeout(() => first.focus(), 30);
  return close;
}

function confirmBox(title, text, okLabel = "Да") {
  return new Promise(resolve => {
    modal({
      title, body: h("p", text), onclose: () => resolve(false),
      actions: [{ label: "Отмена", onclick: () => resolve(false) },
                { label: okLabel, primary: true, onclick: () => resolve(true) }],
    });
  });
}

// 32 hex chars. crypto.randomUUID exists only on https/localhost; getRandomValues works on plain http too.
function randomId() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, "0")).join("");
}

// Russian title -> latin slug for step/track ids
const TRANSLIT = { а: "a", б: "b", в: "v", г: "g", д: "d", е: "e", ё: "e", ж: "zh", з: "z", и: "i", й: "y", к: "k", л: "l", м: "m", н: "n", о: "o", п: "p", р: "r", с: "s", т: "t", у: "u", ф: "f", х: "h", ц: "ts", ч: "ch", ш: "sh", щ: "sch", ъ: "", ы: "y", ь: "", э: "e", ю: "yu", я: "ya" };
function slugify(s) {
  return String(s || "").toLowerCase().split("").map(c => TRANSLIT[c] ?? c).join("")
    .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40) || "step";
}
