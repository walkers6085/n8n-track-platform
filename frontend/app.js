const API = "http://localhost:19001";
let tracks = [];
let selectedTrack = null;
let currentRun = null;

// ---- helpers ----
function toast(msg, ms=2500){
  const el=document.getElementById("toast");
  el.textContent=msg; el.classList.add("show");
  setTimeout(()=>el.classList.remove("show"), ms);
}
async function api(path, opts={}){
  const url = path.startsWith("http") ? path : API + path;
  const res = await fetch(url, {headers:{"Content-Type":"application/json"}, ...opts});
  if(!res.ok){
    const txt = await res.text();
    let detail=txt;
    try{ detail=JSON.parse(txt).detail||txt; }catch{}
    throw new Error(detail||res.statusText);
  }
  const ct=res.headers.get("content-type")||"";
  if(ct.includes("application/json")) return res.json();
  return res.text();
}
function el(tag, cls, text){
  const e=document.createElement(tag);
  if(cls) e.className=cls;
  if(text!==undefined) e.textContent=text;
  return e;
}

// ---- tabs ----
document.querySelectorAll(".tab[data-view]").forEach(b=>{
  b.addEventListener("click",()=>{
    document.querySelectorAll(".tab[data-view]").forEach(x=>x.classList.remove("active"));
    b.classList.add("active");
    document.querySelectorAll(".view").forEach(v=>v.classList.remove("active"));
    document.getElementById("view-"+b.dataset.view).classList.add("active");
    if(b.dataset.view==="admin") loadAdminTracks();
  });
});

// ---- tracks (left panel) ----
async function loadTracks(){
  const box=document.getElementById("track-list");
  try{
    tracks = await api("/api/tracks");
  } catch(e){
    box.innerHTML=`<p style="color:#d92d20;font-size:.85rem">Failed: ${e.message}</p>`;
    return;
  }
  renderTrackList();
}
function renderTrackList(){
  const box=document.getElementById("track-list");
  const filter=(document.getElementById("filter-team").value||"").trim().toLowerCase();
  let filtered=tracks;
  if(filter) filtered=tracks.filter(t=>t.team.toLowerCase().includes(filter));
  if(!filtered.length){ box.innerHTML='<p class="muted">No tracks.</p>'; return; }
  // group by team
  const groups={};
  filtered.forEach(t=>{ (groups[t.team]=groups[t.team]||[]).push(t); });
  box.innerHTML="";
  Object.keys(groups).sort().forEach(team=>{
    const g=el("div","team-group");
    g.appendChild(el("h3",null,team));
    groups[team].forEach(t=>{
      const row=el("div","track-item");
      if(selectedTrack && selectedTrack.team===t.team && selectedTrack.id===t.id) row.classList.add("selected");
      const left=el("div",null,"");
      left.appendChild(el("strong",null,t.name));
      const sub=el("div","muted", `${t.id} · v${t.version}`);
      sub.style.fontSize=".72rem";
      left.appendChild(sub);
      const badge=el("span","badge "+(t.status==="published"?"badge-published":"badge-draft"), t.status);
      row.appendChild(left); row.appendChild(badge);
      row.addEventListener("click",()=> selectTrack(t));
      g.appendChild(row);
    });
    box.appendChild(g);
  });
}
function selectTrack(t){
  selectedTrack=t;
  document.getElementById("btn-start").disabled=false;
  document.getElementById("chat-title").textContent = `${t.name} (${t.team}/${t.id})`;
  renderTrackList();
  renderProgress(t, null);
  // clear chat unless there's an active run for this track
  if(!currentRun || currentRun.track_id!==t.id){
    document.getElementById("messages").innerHTML='<p class="muted">Click "Start run" to begin.</p>';
    document.getElementById("run-id-label").textContent="";
    document.getElementById("run-status").classList.add("hidden");
    document.getElementById("chat-input").disabled=true;
    document.getElementById("btn-send").disabled=true;
  }
}
document.getElementById("filter-team").addEventListener("input", renderTrackList);
document.getElementById("btn-refresh-tracks").addEventListener("click", loadTracks);

// ---- chat flow ----
document.getElementById("btn-start").addEventListener("click", startRun);
document.getElementById("btn-send").addEventListener("click", sendMessage);
document.getElementById("chat-input").addEventListener("keydown", e=>{ if(e.key==="Enter") sendMessage(); });
document.getElementById("btn-advance").addEventListener("click", advanceRun);

async function startRun(){
  if(!selectedTrack) return;
  const btn=document.getElementById("btn-start");
  btn.disabled=true; btn.textContent="Starting...";
  try{
    const run = await api("/api/runs/start",{
      method:"POST",
      body: JSON.stringify({track_id:selectedTrack.id, team:selectedTrack.team, user_id:"demo-user"})
    });
    currentRun=run;
    renderRun(run, selectedTrack);
    toast("Run started");
  } catch(e){ toast("Start failed: "+e.message); }
  finally{ btn.disabled=false; btn.textContent="Start run"; }
}
async function sendMessage(){
  const inp=document.getElementById("chat-input");
  const text=inp.value.trim();
  if(!text || !currentRun) return;
  inp.value="";
  try{
    const run=await api(`/api/runs/${currentRun.run_id}/message`,{
      method:"POST", body: JSON.stringify({text, user_id:"demo-user"})
    });
    currentRun=run;
    const track = tracks.find(t=>t.team===run.team && t.id===run.track_id) || selectedTrack;
    renderRun(run, track);
  }catch(e){ toast(e.message); }
}
async function advanceRun(){
  if(!currentRun) return;
  try{
    const run=await api(`/api/runs/${currentRun.run_id}/advance`,{
      method:"POST", body: JSON.stringify({})
    });
    currentRun=run;
    const track=tracks.find(t=>t.team===run.team && t.id===run.track_id)||selectedTrack;
    renderRun(run,track);
    toast("Advanced");
  }catch(e){ toast(e.message); }
}
function renderRun(run, track){
  document.getElementById("run-id-label").textContent = run.run_id.slice(0,8)+"...";
  const st=document.getElementById("run-status");
  st.textContent=run.status; st.classList.remove("hidden");
  // messages
  const box=document.getElementById("messages");
  box.innerHTML="";
  if(!run.history || !run.history.length){
    box.innerHTML='<p class="muted">No messages yet.</p>';
  } else {
    run.history.forEach(h=>{
      let cls="msg-bot";
      if(h.type==="message" && h.user_id) cls="msg-user";
      else if(h.type==="system") cls="msg-system";
      const d=el("div", "msg "+cls);
      d.textContent=h.text||JSON.stringify(h.payload||"");
      box.appendChild(d);
      if(h.step_id){
        const meta=el("div","msg-meta", h.step_id+" · "+(h.timestamp||"").slice(11,19));
        // attach after msg
        const wrap=el("div",""); wrap.style.display="contents";
      }
    });
  }
  box.scrollTop=box.scrollHeight;
  // enable chat if active
  const active = !["completed","failed","cancelled"].includes(run.status);
  document.getElementById("chat-input").disabled=!active;
  document.getElementById("btn-send").disabled=!active;
  document.getElementById("btn-advance").classList.toggle("hidden", !active);
  if(track) renderProgress(track, run);
}
function renderProgress(track, run){
  const box=document.getElementById("steps-list");
  box.innerHTML="";
  if(!track || !track.steps || !track.steps.length){
    box.innerHTML='<p class="muted">No steps.</p>';
  } else {
    // determine statuses
    let currentId = run ? run.current_step : null;
    let completedSet=new Set();
    if(run){
      // history step_enter/exit gives ordered visits; simple: all steps before current are completed if linear
      const ids=track.steps.map(s=>s.id);
      const curIdx = currentId ? ids.indexOf(currentId) : -1;
      if(run.status==="completed"){
        ids.forEach(id=>completedSet.add(id));
      } else if(curIdx>=0){
        ids.slice(0,curIdx).forEach(id=>completedSet.add(id));
      }
      // also mark steps seen in history as completed if before current
      if(run.history){
        run.history.forEach(h=>{
          if(h.step_id && h.type==="step_exit") completedSet.add(h.step_id);
        });
      }
    }
    track.steps.forEach(s=>{
      const row=el("div","step-row");
      const dot=el("span","step-dot");
      if(completedSet.has(s.id)) dot.classList.add("dot-completed");
      else if(s.id===currentId) dot.classList.add("dot-current");
      else dot.classList.add("dot-pending");
      row.appendChild(dot);
      row.appendChild(el("span","step-name", s.name||s.id));
      row.appendChild(el("span","step-type", s.type));
      box.appendChild(row);
    });
  }
  document.getElementById("vars-display").textContent = JSON.stringify(run?run.variables:track.variables||{}, null, 2);
  const mini=document.getElementById("history-mini");
  if(run && run.history){
    mini.innerHTML = run.history.slice(-6).map(h=>`<div>• ${escapeHtml(h.text||h.type)} <span style="color:#98a2b3">${(h.timestamp||"").slice(11,19)}</span></div>`).join("");
  } else mini.textContent="";
}
function escapeHtml(s){ return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }

// ---- Admin ----
let editingTrack=null;

async function loadAdminTracks(){
  const box=document.getElementById("admin-track-list");
  try{
    const list=await api("/api/tracks");
    tracks=list;
    box.innerHTML="";
    if(!list.length){ box.innerHTML='<p class="muted" style="padding:10px">No tracks yet.</p>'; return; }
    list.forEach(t=>{
      const row=el("div","admin-track-row");
      row.innerHTML=`<div><strong>${escapeHtml(t.name)}</strong> <span class="muted">${escapeHtml(t.team)}/${escapeHtml(t.id)} v${t.version} ${t.status}</span></div>`;
      const btn=el("button","btn btn-sm","Edit");
      btn.addEventListener("click",()=> openEditor(t));
      row.appendChild(btn);
      box.appendChild(row);
    });
    renderTrackList();
  }catch(e){ box.innerHTML=`<p style="color:#d92d20;padding:10px">${e.message}</p>`; }
}
function parseJsonField(id, fallback){
  const v=document.getElementById(id).value.trim();
  if(!v) return fallback;
  return JSON.parse(v);
}
function openEditor(t){
  editingTrack=t;
  document.getElementById("admin-editor").style.display="block";
  document.getElementById("editor-title").textContent = t ? `Edit ${t.team}/${t.id}` : "New Track";
  document.getElementById("ed-id").value = t? t.id : "";
  document.getElementById("ed-id").disabled = !!t;
  document.getElementById("ed-team").value = t? t.team : "";
  document.getElementById("ed-team").disabled = !!t;
  document.getElementById("ed-name").value = t? t.name : "";
  document.getElementById("ed-status").value = t? t.status : "draft";
  document.getElementById("ed-desc").value = t? (t.description||"") : "";
  document.getElementById("ed-steps").value = t? JSON.stringify(t.steps||[], null, 2) : "[]";
  document.getElementById("ed-transitions").value = t? JSON.stringify(t.transitions||[], null, 2) : "[]";
  document.getElementById("ed-vars").value = t? JSON.stringify(t.variables||{}, null, 2) : "{}";
  document.getElementById("ed-llm").value = t && t.llm_config ? JSON.stringify(t.llm_config, null, 2) : "";
  renderGraphFromEditor();
  renderStepCards();
  renderTransitionsEditor();
  document.getElementById("admin-editor").scrollIntoView({behavior:"smooth"});
}
function closeEditor(){
  document.getElementById("admin-editor").style.display="none";
  editingTrack=null;
}
document.getElementById("btn-new-track").addEventListener("click",()=> openEditor(null));
document.getElementById("btn-refresh-admin").addEventListener("click", loadAdminTracks);
document.getElementById("btn-cancel-edit").addEventListener("click", closeEditor);
document.getElementById("btn-save-track").addEventListener("click", saveTrack);
document.getElementById("btn-publish").addEventListener("click", publishTrack);
document.getElementById("btn-delete-track").addEventListener("click", deleteTrack);
document.getElementById("btn-add-transition").addEventListener("click", ()=>{
  const arr=parseJsonField("ed-transitions", []);
  arr.push({from: "", to: "", label:""});
  document.getElementById("ed-transitions").value=JSON.stringify(arr,null,2);
  renderTransitionsEditor(); renderGraphFromEditor();
});
["ed-steps","ed-transitions"].forEach(id=>{
  document.getElementById(id).addEventListener("input", ()=>{ renderGraphFromEditor(); renderStepCards(); renderTransitionsEditor(); });
});

async function saveTrack(){
  let steps, transitions, variables, llm;
  try{
    steps=parseJsonField("ed-steps", []);
    transitions=parseJsonField("ed-transitions", []);
    variables=parseJsonField("ed-vars", {});
    const llmRaw=document.getElementById("ed-llm").value.trim();
    llm = llmRaw ? JSON.parse(llmRaw) : null;
  }catch(e){ toast("Invalid JSON: "+e.message); return; }
  const body={
    id: document.getElementById("ed-id").value.trim(),
    team: document.getElementById("ed-team").value.trim(),
    name: document.getElementById("ed-name").value.trim(),
    description: document.getElementById("ed-desc").value.trim()||null,
    steps, transitions, variables,
    status: document.getElementById("ed-status").value,
  };
  if(llm) body.llm_config=llm;
  if(!body.id || !body.team || !body.name){ toast("id, team, name required"); return; }
  try{
    const saved=await api("/api/tracks",{method:"POST", body:JSON.stringify(body)});
    toast(`Saved ${saved.team}/${saved.id} v${saved.version}`);
    await loadAdminTracks();
    openEditor(saved);
  }catch(e){ toast("Save failed: "+e.message); }
}
async function publishTrack(){
  const team=document.getElementById("ed-team").value.trim();
  const id=document.getElementById("ed-id").value.trim();
  if(!team||!id){ toast("Select a track first"); return; }
  try{
    const t=await api(`/api/tracks/${team}/${id}/publish`,{method:"POST"});
    toast(`Published v${t.version}`);
    await loadAdminTracks();
  }catch(e){ toast(e.message); }
}
async function deleteTrack(){
  const team=document.getElementById("ed-team").value.trim();
  const id=document.getElementById("ed-id").value.trim();
  if(!team||!id) return;
  // business rule note: published track cannot be deleted (enforced client-side + server should reject)
  const cur = tracks.find(x=>x.team===team && x.id===id);
  if(cur && cur.status==="published"){
    toast("Published track cannot be deleted — archive it first");
    return;
  }
  if(!confirm(`Delete ${team}/${id}?`)) return;
  try{
    await api(`/api/tracks/${team}/${id}`,{method:"DELETE"});
    toast("Deleted");
    closeEditor();
    await loadAdminTracks();
  }catch(e){ toast(e.message); }
}

// ---- graph (simple SVG) ----
function renderGraphFromEditor(){
  const svg=document.getElementById("graph-svg");
  // clear except defs
  const defs=svg.querySelector("defs");
  svg.innerHTML=""; svg.appendChild(defs);
  let steps, trans;
  try{
    steps=JSON.parse(document.getElementById("ed-steps").value.trim()||"[]");
    trans=JSON.parse(document.getElementById("ed-transitions").value.trim()||"[]");
  }catch{ return; }
  if(!steps.length) return;
  const W=900, H=320, pad=30;
  const n=steps.length;
  const gap=(W-2*pad)/Math.max(1,n-1);
  const y=150;
  const pos={};
  steps.forEach((s,i)=>{
    if(s.position && typeof s.position.x==="number" && typeof s.position.y==="number"){
      pos[s.id]={x: s.position.x, y: s.position.y};
    } else {
      pos[s.id]={x: pad + i*gap, y};
    }
  });
  // edges first
  trans.forEach(t=>{
    const a=pos[t.from], b=pos[t.to];
    if(!a||!b) return;
    const path=document.createElementNS("http://www.w3.org/2000/svg","path");
    const mx=(a.x+b.x)/2, my=(a.y+b.y)/2 - 18;
    // simple curved
    path.setAttribute("d", `M ${a.x+45} ${a.y} Q ${mx} ${my} ${b.x-45} ${b.y}`);
    path.setAttribute("class","edge");
    svg.appendChild(path);
    if(t.label||t.condition){
      const txt=document.createElementNS("http://www.w3.org/2000/svg","text");
      txt.setAttribute("x", mx); txt.setAttribute("y", my);
      txt.setAttribute("text-anchor","middle");
      txt.setAttribute("class","edge-label");
      txt.textContent=t.label||t.condition||"";
      svg.appendChild(txt);
    }
  });
  // nodes
  steps.forEach(s=>{
    const p=pos[s.id];
    const g=document.createElementNS("http://www.w3.org/2000/svg","g");
    const rect=document.createElementNS("http://www.w3.org/2000/svg","rect");
    rect.setAttribute("x", p.x-45); rect.setAttribute("y", p.y-22);
    rect.setAttribute("width","90"); rect.setAttribute("height","44");
    rect.setAttribute("class","step-card");
    g.appendChild(rect);
    const t1=document.createElementNS("http://www.w3.org/2000/svg","text");
    t1.setAttribute("x", p.x); t1.setAttribute("y", p.y-4);
    t1.setAttribute("text-anchor","middle"); t1.setAttribute("class","step-label");
    t1.textContent=(s.name||s.id).slice(0,14);
    g.appendChild(t1);
    const t2=document.createElementNS("http://www.w3.org/2000/svg","text");
    t2.setAttribute("x", p.x); t2.setAttribute("y", p.y+10);
    t2.setAttribute("text-anchor","middle"); t2.setAttribute("class","edge-label");
    t2.textContent=s.type;
    g.appendChild(t2);
    svg.appendChild(g);
  });
}
function renderStepCards(){
  const box=document.getElementById("step-cards");
  let steps;
  try{ steps=JSON.parse(document.getElementById("ed-steps").value.trim()||"[]"); }catch{ box.innerHTML='<p class="muted">Invalid JSON</p>'; return; }
  box.innerHTML="";
  steps.forEach((s,i)=>{
    const card=el("div","");
    card.style.cssText="border:1px solid #e2e4e7;border-radius:8px;padding:8px;background:#fff";
    card.innerHTML=`<div style="font-weight:600;font-size:.84rem">${escapeHtml(s.name||s.id)} <span class="step-type">${escapeHtml(s.type)}</span></div>
      <div class="muted" style="font-size:.75rem">id: ${escapeHtml(s.id)}</div>
      <pre style="font-size:.7rem;background:#f9fafb;padding:4px;border-radius:4px;overflow:auto;max-height:80px">${escapeHtml(JSON.stringify(s.config||{},null,2))}</pre>`;
    box.appendChild(card);
  });
  if(!steps.length) box.innerHTML='<p class="muted">No steps.</p>';
}
function renderTransitionsEditor(){
  const box=document.getElementById("transitions-editor");
  let trans;
  try{ trans=JSON.parse(document.getElementById("ed-transitions").value.trim()||"[]"); }catch{ box.innerHTML='<p class="muted">Invalid JSON</p>'; return; }
  box.innerHTML="";
  if(!trans.length){ box.innerHTML='<p class="muted">No transitions.</p>'; return; }
  trans.forEach((t,i)=>{
    const row=el("div","");
    row.style.cssText="display:flex;gap:6px;align-items:center;margin-bottom:6px;flex-wrap:wrap";
    row.innerHTML=`<span style="font-size:.78rem">${i+1}.</span>
      <input data-i="${i}" data-k="from" value="${escapeHtml(t.from||"")}" placeholder="from" style="flex:1;min-width:80px;padding:5px;border:1px solid #e2e4e7;border-radius:6px;font-size:.82rem">
      <span>→</span>
      <input data-i="${i}" data-k="to" value="${escapeHtml(t.to||"")}" placeholder="to" style="flex:1;min-width:80px;padding:5px;border:1px solid #e2e4e7;border-radius:6px;font-size:.82rem">
      <input data-i="${i}" data-k="label" value="${escapeHtml(t.label||"")}" placeholder="label" style="flex:1;min-width:80px;padding:5px;border:1px solid #e2e4e7;border-radius:6px;font-size:.82rem">
      <input data-i="${i}" data-k="condition" value="${escapeHtml(t.condition||"")}" placeholder="condition" style="flex:1;min-width:120px;padding:5px;border:1px solid #e2e4e7;border-radius:6px;font-size:.82rem">`;
    const del=el("button","btn btn-sm","✕");
    del.addEventListener("click",()=>{
      const arr=JSON.parse(document.getElementById("ed-transitions").value);
      arr.splice(i,1);
      document.getElementById("ed-transitions").value=JSON.stringify(arr,null,2);
      renderTransitionsEditor(); renderGraphFromEditor();
    });
    row.appendChild(del);
    row.querySelectorAll("input").forEach(inp=>{
      inp.addEventListener("input",()=>{
        try{
          const arr=JSON.parse(document.getElementById("ed-transitions").value);
          arr[parseInt(inp.dataset.i)][inp.dataset.k]=inp.value;
          document.getElementById("ed-transitions").value=JSON.stringify(arr,null,2);
          renderGraphFromEditor();
        }catch{}
      });
    });
    box.appendChild(row);
  });
}

// ---- LLM config ----
async function loadLLM(){
  try{
    const cfg=await api("/api/config/llm");
    document.getElementById("llm-provider").value=cfg.provider||"mistral";
    document.getElementById("llm-model").value=cfg.model||"mistral-small-latest";
    document.getElementById("llm-temp").value=cfg.temperature??0.7;
    document.getElementById("llm-max").value=cfg.max_tokens||"";
    document.getElementById("llm-base").value=cfg.base_url||"";
    document.getElementById("llm-key").value=String(!!cfg.api_key_configured);
  }catch(e){ toast("LLM load failed: "+e.message); }
}
async function saveLLM(){
  const body={
    provider: document.getElementById("llm-provider").value.trim()||"mistral",
    model: document.getElementById("llm-model").value.trim()||"mistral-small-latest",
    temperature: parseFloat(document.getElementById("llm-temp").value)||0.7,
    max_tokens: document.getElementById("llm-max").value.trim()? parseInt(document.getElementById("llm-max").value): null,
    base_url: document.getElementById("llm-base").value.trim()||null,
    api_key_configured: document.getElementById("llm-key").value==="true",
    extra:{}
  };
  try{
    await api("/api/config/llm",{method:"PUT", body:JSON.stringify(body)});
    toast("LLM config saved");
  }catch(e){ toast(e.message); }
}
document.getElementById("btn-load-llm").addEventListener("click", loadLLM);
document.getElementById("btn-save-llm").addEventListener("click", saveLLM);

// ---- init ----
loadTracks();
loadLLM();
