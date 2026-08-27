#!/usr/bin/env python3
"""Deploy Track Engine workflow to n8n (port 5679) for n8n-track-platform.

Creates/updates ONE universal Track Engine workflow + Track Event Logger helper.
Does NOT delete existing workflows (unlike /home/deb/n8n/workflows/deploy.py).

Reuses MISTRAL_CREDENTIAL_ID and helper patterns from /home/deb/n8n/workflows/deploy.py
"""

import json
import uuid
import subprocess
import sys
import os
import time
from pathlib import Path

N8N_URL = "http://localhost:5679"
MISTRAL_CREDENTIAL_ID = "fSvUz4eWB05p1bWM"
COOKIE_FILE = "/tmp/n8n_cookies.txt"
FILE_SERVER_PORT = 19099
BACKEND_PORT = 19001
# Inside docker n8n container, host is 172.17.0.1 or 172.20.0.1; try 172.20.0.1 first, fallback is host.docker.internal
FS = f"http://172.20.0.1:{FILE_SERVER_PORT}"
BACKEND = f"http://172.20.0.1:{BACKEND_PORT}"
# Fallback URLs accessible from host
FS_HOST = f"http://localhost:{FILE_SERVER_PORT}"
BACKEND_HOST = f"http://localhost:{BACKEND_PORT}"

SKILL_DIR = Path("/home/deb/n8n/skills")
SKILL_MAP = {
    "strategist": "strateg",
    "market_analyst": "market-analyst",
    "news_analyst": "news-analyst",
    "editor": "editor",
    "publisher": "publisher",
    "trigger_publisher": "trigger-publisher",
    "performance_analyst": "performance-analyst",
}

def load_skill(name):
    p = SKILL_DIR / SKILL_MAP[name] / "SKILL.MD"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return ""

UID = lambda: uuid.uuid4().hex[:8]

# ---------------------------------------------------------------------------
# n8n node helpers (reused from /home/deb/n8n/workflows/deploy.py)
# ---------------------------------------------------------------------------

def code_node(name, code, pos):
    return {"id": UID(), "name": name, "type": "n8n-nodes-base.code", "typeVersion": 2, "position": pos,
            "parameters": {"jsCode": code}}

def schedule_node(name, rule, pos):
    return {"id": UID(), "name": name, "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.3,
            "position": pos, "parameters": {"rule": {"interval": [rule]}}}

def chain_llm_node(name, prompt, pos):
    return {"id": UID(), "name": name, "type": "@n8n/n8n-nodes-langchain.chainLlm", "typeVersion": 1.9,
            "position": pos, "parameters": {"promptType": "define", "text": prompt,
            "messages": {"messageValues": [{"message": prompt}]}}}

def mistral_node(name, model, pos, max_tokens=16384, temperature=0.3):
    opts = {"maxTokens": max_tokens}
    if temperature is not None:
        opts["temperature"] = temperature
    return {"id": UID(), "name": name, "type": "@n8n/n8n-nodes-langchain.lmChatMistralCloud",
            "typeVersion": 1, "position": pos, "parameters": {"model": model, "options": opts},
            "credentials": {"mistralCloudApi": {"id": MISTRAL_CREDENTIAL_ID, "name": "Mistral Cloud account"}}}

STRICT_PREFIX = """Ты — intent-extractor для Track Engine. Текущая дата: {{$now.format('yyyy-MM-dd')}}.

ПРАВИЛА:
1. Только русский/English как в сообщении пользователя.
2. Никаких пояснений, шагов, предисловий.
3. Только чистый JSON. Без ```, markdown.
4. Строго валидный JSON.
"""

def llm_pair(name_prefix, skill_key, pc, pm, max_tokens=16384, temperature=0.3):
    s = load_skill(skill_key)
    if s:
        p = STRICT_PREFIX + "\n\n" + s + "\n\n**Данные:**\n{{JSON.stringify($json)}}"
    else:
        # Generic intent extraction prompt for Track Engine
        p = STRICT_PREFIX + "\n\nТы извлекаешь переменные из сообщения пользователя для шага типа input/question/approval. Верни JSON {\"variables\": {key: value}, \"intent\": \"approve|reject|provide\"}.\n\n**Данные шага:**\n{{JSON.stringify($json)}}"
    chain = chain_llm_node(name_prefix, p, pc)
    model = mistral_node(f"{name_prefix} Model", "mistral-small-latest", pm, max_tokens, temperature)
    extract = code_node(f"Extract {name_prefix}", LLM_TEXT, [pc[0] + 200, pc[1]])
    return [chain, model, extract]

def llm_connections(name_prefix, next_node):
    return {
        f"{name_prefix} Model": {"ai_languageModel": [[{"node": name_prefix, "type": "ai_languageModel", "index": 0}]]},
        name_prefix: {"main": [[{"node": f"Extract {name_prefix}", "type": "main", "index": 0}]]},
        f"Extract {name_prefix}": {"main": [[{"node": next_node, "type": "main", "index": 0}]]},
    }

def http_get_node(name, url, pos):
    return {"id": UID(), "name": name, "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": pos,
            "parameters": {"method": "GET", "url": url, "options": {}, "sendBody": False, "sendQuery": False,
            "sendHeaders": False, "responseFormat": "autodetect"}}

def http_post_node(name, url, pos, json_body=None):
    if json_body:
        return {"id": UID(), "name": name, "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": pos,
                "parameters": {"method": "POST", "url": url, "options": {}, "sendBody": True, "sendQuery": False,
                "sendHeaders": False, "responseFormat": "autodetect",
                "contentType": "json", "specifyBody": "json", "jsonBody": json_body}}
    return {"id": UID(), "name": name, "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": pos,
            "parameters": {"method": "POST", "url": url, "options": {}, "sendBody": True, "sendQuery": False,
            "sendHeaders": False, "responseFormat": "autodetect",
            "contentType": "json", "specifyBody": "json", "jsonBody": "={{ $json }}"}}

def webhook_node(name, pos, path, method="POST", response_mode="responseNode"):
    wh_id = path
    opts = {}
    if response_mode:
        opts = {"responseMode": response_mode}
    return {"id": UID(), "name": name, "type": "n8n-nodes-base.webhook", "typeVersion": 1, "position": pos,
            "parameters": {"path": path, "httpMethod": method, "options": opts}, "webhookId": wh_id}

def switch_node(name, pos, rules, fallback_output=5):
    # n8n switch node: mode rules
    return {"id": UID(), "name": name, "type": "n8n-nodes-base.switch", "typeVersion": 3.2, "position": pos,
            "parameters": {
                "rules": {"values": rules},
                "options": {"fallbackOutput": "extra", "allMatch": False}
            }}

def merge_node(name, pos):
    return {"id": UID(), "name": name, "type": "n8n-nodes-base.merge", "typeVersion": 2, "position": pos,
            "parameters": {"mode": "combine", "combinationMode": "mergeByPosition"}}

def respond_node(name, pos, respond_with="json", response_data="{{ $json }}"):
    return {"id": UID(), "name": name, "type": "n8n-nodes-base.respondToWebhook", "typeVersion": 1.1, "position": pos,
            "parameters": {"respondWith": respond_with, "responseData": response_data, "options": {}}}

def make_wf(name, nodes, connections):
    return {"name": name, "nodes": nodes, "connections": connections,
            "settings": {"saveManualExecutions": True, "callerPolicy": "workflowsFromSameOwner"},
            "staticData": None, "tags": []}

# ---------------------------------------------------------------------------
# Code snippets for Track Engine
# ---------------------------------------------------------------------------

LLM_TEXT = """const raw = $json.text 
  || $json.message?.additional_kwargs?.reasoning 
  || $json.message?.reasoning 
  || $json.output 
  || (typeof $json.message?.content === 'string' ? $json.message.content : '')
  || '';
return [{ text: raw }]"""

LOAD_SESSION_CODE = """// Input: webhook body {team, track_id, run_id, user_id, message, variables, action}
// Output: normalized session {team, track_id, run_id, user_id, message, variables}
const body = $input.first().json.body || $input.first().json || {};
// Support both {body: {...}} and flat
const b = body.body || body;
const team = b.team || body.team || $json.team || 'team-a';
const track_id = b.track_id || b.trackId || body.track_id || 'release-installation';
const run_id = b.run_id || b.runId || body.run_id || null;
const user_id = b.user_id || b.userId || body.user_id || 'anonymous';
const message = b.message || b.text || body.message || '';
const variables = b.variables || body.variables || {};
const action = b.action || 'message';

// Persist for next nodes via $json
return [{
  team,
  track_id,
  run_id,
  user_id,
  message,
  variables,
  action,
  _raw_body: b,
  _timestamp: new Date().toISOString()
}];"""

GET_CURRENT_STEP_CODE = """// Inputs: track.json + run.json merged
// We receive two HTTP GET outputs merged by position. Use $node references.
// Simpler: this node receives the last HTTP GET output, but we need both track and run.
// n8n Merge combined them, so $json contains {track, run} or array.
// This code resolves current step object.
const track = $node['Load Track'].json || {};
const run = $node['Load Run'].json || {};
// Fallback: if run not found (new run), create stub
let currentStepId = run.current_step || null;
let steps = track.steps || [];
let step = null;
if (currentStepId) {
  step = steps.find(s => s.id === currentStepId) || null;
} else if (steps.length > 0) {
  currentStepId = steps[0].id;
  step = steps[0];
}
const variables = {...(track.variables||{}), ...(run.variables||{}), ...($json.variables||{})};
return [{
  team: $json.team || run.team || track.team,
  track_id: track.id || $json.track_id,
  run_id: $json.run_id || run.run_id,
  user_id: $json.user_id || run.user_id,
  message: $json.message,
  variables,
  track,
  run,
  current_step_id: currentStepId,
  current_step: step,
  step_type: step ? step.type : 'message',
  step_config: step ? step.config : {},
  _timestamp: $json._timestamp
}];"""

HANDLE_MESSAGE_CODE = """// step.type == message — return the message text with variable interpolation
const step = $json.current_step || {};
const config = step.config || {};
let text = config.text || config.message || 'No message configured';
const vars = $json.variables || {};
// Simple mustache interpolation {{variables.x}}
text = text.replace(/\\{\\{\\s*variables\\.([^\\}]+)\\s*\\}\\}/g, (_, k) => vars[k.trim()] ?? '');
text = text.replace(/\\{\\{\\s*([^\\}]+)\\s*\\}\\}/g, (_, k) => vars[k.trim()] ?? text);
return [{
  team: $json.team,
  track_id: $json.track_id,
  run_id: $json.run_id,
  user_id: $json.user_id,
  message: $json.message,
  variables: $json.variables,
  track: $json.track,
  run: $json.run,
  current_step_id: $json.current_step_id,
  current_step: $json.current_step,
  step_type: $json.step_type,
  result: { type: 'message', text },
  output_text: text,
  _handled: 'message'
}];"""

PREPARE_LLM_INPUT_CODE = """// Prepare prompt for intent extraction
const step = $json.current_step || {};
const config = step.config || {};
const prompt = config.prompt || config.text || 'Please provide input:';
const variable = config.variable || 'input';
const userMessage = $json.message || '';
return [{
  team: $json.team,
  track_id: $json.track_id,
  run_id: $json.run_id,
  user_id: $json.user_id,
  message: userMessage,
  variables: $json.variables,
  track: $json.track,
  run: $json.run,
  current_step_id: $json.current_step_id,
  current_step: $json.current_step,
  step_type: $json.step_type,
  prompt,
  variable,
  user_input: userMessage,
  instruction: `Extract variable \"${variable}\" from user message. User said: \"${userMessage}\". Prompt was: \"${prompt}\". Return JSON {\"variables\": {\"${variable}\": \"value\"}, \"intent\": \"provide\"}. For approval steps, intent is approve/reject.`
}];"""

EXTRACT_VARIABLES_CODE = """let t = $json.text || $json.output || '{}';
t = t.replace(/^```(?:json)?\\s*\\n?/g,'').replace(/\\n?\\s*```$/g,'');
let parseJSON = (s) => { try { return JSON.parse(s) } catch(e){ return null } };
let o = parseJSON(t);
if (!o) {
  let f = t.replace(/(\"\\w+\")\\s+(\\[|\\{)/g,'$1\": $2');
  o = parseJSON(f);
}
if (!o) {
  // Fallback: treat raw text as variable value
  const step = $node['Get Current Step'].json || {};
  const varName = step.current_step?.config?.variable || $json.variable || 'input';
  o = { variables: { [varName]: ($json.user_input || t) }, intent: 'provide' };
}
let variables = o.variables || o || {};
// Normalize approval intent
if (o.intent === 'approve') variables.approved = true;
if (o.intent === 'reject') variables.approved = false;
// Also handle textual approval
const msg = ($node['Get Current Step'].json?.message || '').toLowerCase();
if (msg.includes('approve') || msg.includes('да') || msg === 'yes') variables.approved = true;
if (msg.includes('reject') || msg.includes('нет') || msg === 'no') variables.approved = false;

const prev = $node['Get Current Step'].json || {};
const mergedVars = { ...(prev.variables||{}), ...variables };
// Keep last message
mergedVars._last_message = prev.message || '';
// Also set the step's variable directly if present
const stepVar = prev.current_step?.config?.variable;
if (stepVar && !mergedVars[stepVar] && prev.message) {
  mergedVars[stepVar] = prev.message;
}

return [{
  team: prev.team,
  track_id: prev.track_id,
  run_id: prev.run_id,
  user_id: prev.user_id,
  message: prev.message,
  variables: mergedVars,
  track: prev.track,
  run: prev.run,
  current_step_id: prev.current_step_id,
  current_step: prev.current_step,
  step_type: prev.step_type,
  result: { type: 'input', variables },
  output_text: `Captured: ${JSON.stringify(variables)}`,
  _handled: 'input',
  _llm_raw: t
}];"""

JIRA_MOCK_CODE = """// After HTTP Request to Jira (or mock), normalize response
const jiraResp = $json || {};
const step = $node['Get Current Step'].json || {};
// If HTTP failed, mock a successful response
let issue = jiraResp.fields ? jiraResp : (jiraResp.jira_issue || { fields: { status: { name: 'Ready for Release' }, summary: 'Mock issue' }, key: step.variables?.story_key || 'PROJ-123' });
if (!issue.fields) issue = { fields: { status: { name: 'Ready for Release' } }, key: 'MOCK-1' };
const vars = { ...(step.variables||{}), jira_issue: issue };
return [{
  team: step.team,
  track_id: step.track_id,
  run_id: step.run_id,
  user_id: step.user_id,
  message: step.message,
  variables: vars,
  track: step.track,
  run: step.run,
  current_step_id: step.current_step_id,
  current_step: step.current_step,
  step_type: step.step_type,
  result: { type: 'jira', issue },
  output_text: `Jira fetched: ${issue.key || step.variables?.story_key}`,
  _handled: 'jira'
}];"""

VALIDATE_CODE = """// step.type == validation
const step = $json; // contains track, run, current_step, etc if came via Switch
const src = $node['Get Current Step'].json || $json;
const cfg = src.current_step?.config || {};
const rules = cfg.rules || [];
let ok = true;
let errors = [];
const vars = src.variables || {};
for (const r of rules) {
  const field = r.field || '';
  // Resolve field like variables.x.y
  let val;
  if (field.startsWith('variables.')) {
    const path = field.replace('variables.','').split('.');
    let cur = vars;
    for (const p of path) cur = cur?.[p];
    val = cur;
  } else {
    val = vars[field];
  }
  const op = r.operator || 'equals';
  const expected = r.value;
  let pass = false;
  if (op === 'equals') pass = String(val) === String(expected);
  else if (op === 'regex') { try { pass = new RegExp(expected).test(String(val||'')) } catch(e){ pass=false } }
  else if (op === 'in') pass = Array.isArray(expected) ? expected.includes(val) : String(expected).split(',').includes(String(val));
  else pass = !!val;
  if (!pass) { ok=false; errors.push(r.message || `Rule failed: ${field} ${op} ${expected} (got ${val})`); }
}
return [{
  team: src.team,
  track_id: src.track_id,
  run_id: src.run_id,
  user_id: src.user_id,
  message: src.message,
  variables: { ...vars, _validation_ok: ok, _validation_errors: errors },
  track: src.track,
  run: src.run,
  current_step_id: src.current_step_id,
  current_step: src.current_step,
  step_type: src.step_type,
  result: { type: 'validation', ok, errors },
  output_text: ok ? 'Validation passed' : 'Validation failed: ' + errors.join('; '),
  _handled: 'validation',
  _validation_ok: ok
}];"""

CONDITION_CODE = """// step.type == condition — evaluate expression
const src = $node['Get Current Step'].json || $json;
const cfg = src.current_step?.config || {};
let expr = cfg.expression || cfg.condition || '';
const vars = src.variables || {};
let result = false;
let error = null;
try {
  // Safe evaluator: supports variables.x == 'value', variables.x != 'value', and bare truthiness
  const cond = expr.trim();
  if (!cond) result = true;
  else if (cond.includes('==')) {
    const [l,r] = cond.split('==').map(s=>s.trim().replace(/^['\"]|['\"]$/g,''));
    const varName = l.replace('variables.','');
    const expected = r.toLowerCase();
    const actual = String(vars[varName] ?? '').toLowerCase();
    result = actual === expected;
  } else if (cond.includes('!=')) {
    const [l,r] = cond.split('!=').map(s=>s.trim().replace(/^['\"]|['\"]$/g,''));
    const varName = l.replace('variables.','');
    const expected = r.toLowerCase();
    const actual = String(vars[varName] ?? '').toLowerCase();
    result = actual !== expected;
  } else {
    const key = cond.replace('variables.','');
    result = !!vars[key];
  }
} catch(e){ error = String(e); }
return [{
  team: src.team,
  track_id: src.track_id,
  run_id: src.run_id,
  user_id: src.user_id,
  message: src.message,
  variables: { ...vars, _condition_result: result },
  track: src.track,
  run: src.run,
  current_step_id: src.current_step_id,
  current_step: src.current_step,
  step_type: src.step_type,
  result: { type: 'condition', expression: expr, result },
  output_text: `Condition \"${expr}\" => ${result}`,
  _handled: 'condition',
  _condition_result: result,
  _error: error
}];"""

ACTION_STUB_CODE = """// step.type == action/llm/wait/webhook — stub execution
const src = $node['Get Current Step'].json || $json;
const cfg = src.current_step?.config || {};
const action = cfg.action || src.step_type || 'action';
const params = cfg.params || {};
const vars = src.variables || {};
// Mock execution: set install_status=success for install actions
let newVars = { ...vars };
if (action.includes('install') || action.includes('rollback') || action.includes('deploy') || action.includes('canary')) {
  newVars.install_status = 'success';
  newVars.rollback_status = 'success';
}
newVars._last_action = action;
newVars._last_action_params = params;
return [{
  team: src.team,
  track_id: src.track_id,
  run_id: src.run_id,
  user_id: src.user_id,
  message: src.message,
  variables: newVars,
  track: src.track,
  run: src.run,
  current_step_id: src.current_step_id,
  current_step: src.current_step,
  step_type: src.step_type,
  result: { type: 'action', action, params, status: 'success' },
  output_text: `Action \"${action}\" executed (stub)`,
  _handled: 'action'
}];"""

SAVE_STATE_CODE = """// Converge branch outputs — persist incremental state before transition
// Input comes from one of the branch handlers (message/input/jira/validation/condition/action)
// Normalize to single object with merged variables
const src = $json;
const base = $node['Get Current Step'].json || {};
// src may already contain merged variables; prefer it
const variables = src.variables || base.variables || {};
const result = src.result || {};
// Build history entry
const entry = {
  step_id: base.current_step_id,
  type: src._handled || base.step_type || 'system',
  text: src.output_text || result.text || JSON.stringify(result).slice(0,500),
  payload: { result, config: base.current_step?.config },
  timestamp: new Date().toISOString()
};
return [{
  team: base.team,
  track_id: base.track_id,
  run_id: base.run_id,
  user_id: base.user_id,
  message: base.message,
  variables,
  track: base.track,
  run: base.run,
  current_step_id: base.current_step_id,
  current_step: base.current_step,
  step_type: base.step_type,
  history_entry: entry,
  result,
  output_text: src.output_text || '',
  _handled: src._handled || 'unknown'
}];"""

DETERMINE_TRANSITION_CODE = """// Determine next step via transitions + conditions
const src = $json;
const track = src.track || {};
const run = src.run || {};
let variables = src.variables || {};
const currentId = src.current_step_id;
const transitions = track.transitions || [];
const steps = track.steps || [];

// Find outgoing transitions
let outgoing = transitions.filter(t => (t.from || t.from_step) === currentId);
let nextId = null;
let matched = null;

if (outgoing.length === 0) {
  // Linear fallback
  const ids = steps.map(s=>s.id);
  const idx = ids.indexOf(currentId);
  if (idx >=0 && idx+1 < ids.length) {
    nextId = ids[idx+1];
    matched = { to: nextId, label: 'linear' };
  }
} else {
  for (const t of outgoing) {
    const cond = (t.condition || '').trim();
    if (!cond) { // unconditional — take first unconditional if none matched yet
      if (!nextId) { nextId = t.to; matched = t; }
      continue;
    }
    // Evaluate condition same as backend _next_step
    let ok = false;
    if (cond.includes('==')) {
      const parts = cond.split('==').map(s=>s.trim().replace(/^['\"]|['\"]$/g,''));
      const varName = parts[0].replace('variables.','');
      const expected = parts[1].toLowerCase();
      const actual = String(variables[varName] ?? '').toLowerCase();
      ok = actual === expected;
    } else if (cond.includes('!=')) {
      const parts = cond.split('!=').map(s=>s.trim().replace(/^['\"]|['\"]$/g,''));
      const varName = parts[0].replace('variables.','');
      const expected = parts[1].toLowerCase();
      const actual = String(variables[varName] ?? '').toLowerCase();
      ok = actual !== expected;
    } else {
      const key = cond.replace('variables.','');
      ok = !!variables[key];
    }
    if (ok) { nextId = t.to; matched = t; break; }
  }
  // If no condition matched, fallback to first unconditional
  if (!nextId && outgoing.some(t=>!t.condition)) {
    const fallback = outgoing.find(t=>!t.condition);
    nextId = fallback.to; matched = fallback;
  }
}

// Build updated run payload
let status = run.status || 'active';
let current_step = nextId || null;
if (!nextId) {
  status = 'completed';
  current_step = null;
}

// Find next step object for response
let nextStep = null;
if (nextId) nextStep = steps.find(s=>s.id===nextId) || null;
let nextText = null;
if (nextStep) {
  nextText = nextStep.config?.text || nextStep.config?.prompt || `Entered ${nextStep.name || nextStep.id} (${nextStep.type})`;
  // Interpolate
  nextText = nextText.replace(/\\{\\{\\s*variables\\.([^\\}]+)\\s*\\}\\}/g, (_, k) => variables[k.trim()] ?? '');
}

const historyEntry = src.history_entry || null;
let history = run.history || [];
if (historyEntry) history.push(historyEntry);
if (nextId && nextStep && nextStep.type === 'message' && nextText) {
  history.push({ step_id: nextId, type: 'step_enter', text: nextText, payload: nextStep.config, timestamp: new Date().toISOString() });
}

const now = new Date().toISOString();
const updatedRun = {
  run_id: src.run_id || run.run_id,
  team: src.team,
  track_id: src.track_id,
  track_version: run.track_version || track.version || 1,
  user_id: src.user_id || run.user_id,
  current_step,
  status,
  variables,
  history,
  created_at: run.created_at || now,
  updated_at: now
};

return [{
  team: src.team,
  track_id: src.track_id,
  run_id: src.run_id,
  updatedRun,
  next_step_id: nextId,
  next_step: nextStep,
  next_text: nextText,
  matched_transition: matched,
  output_text: src.output_text || '',
  status,
  variables
}];"""

# ---------------------------------------------------------------------------
# Event Logger workflow Code
# ---------------------------------------------------------------------------
EVENT_LOGGER_CODE = """const body = $input.first().json.body || $input.first().json || {};
const now = new Date().toISOString();
const entry = {
  timestamp: now,
  event: body.event || 'track_event',
  team: body.team || 'unknown',
  track_id: body.track_id || body.trackId || 'unknown',
  run_id: body.run_id || body.runId || null,
  step_id: body.step_id || body.stepId || null,
  user_id: body.user_id || body.userId || null,
  payload: body.payload || body
};
console.log(JSON.stringify(entry));
// Return for webhook response
return [{ logged: true, entry, timestamp: now }];"""

# ---------------------------------------------------------------------------
# Helpers to start file server and deploy logic
# ---------------------------------------------------------------------------

def start_file_server():
    pids = [l for l in os.popen("ps aux").readlines() if "n8n-track-platform/files/file_server.py" in l]
    if not pids:
        subprocess.Popen(["python3", "/home/deb/n8n-track-platform/files/file_server.py"],
            stdout=open("/tmp/n8n_track_file_server.log", "a"), stderr=subprocess.STDOUT, start_new_session=True)
        time.sleep(1)
        print("  Track file server started on 19099")

def n8n_request(method, path, data=None):
    cmd = ["curl", "-s", "-b", COOKIE_FILE, "-X", method, f"{N8N_URL}{path}",
           "-H", "Content-Type: application/json", "-H", "X-CSRF-Token: cookie"]
    if data is not None:
        cmd += ["-d", json.dumps(data)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    try:
        return json.loads(r.stdout) if r.stdout.strip() else {}
    except:
        return {"raw": r.stdout[:2000], "stderr": r.stderr[:500] if hasattr(r, 'stderr') else ''}

def find_workflow_by_name(name):
    resp = n8n_request("GET", "/rest/workflows")
    data = resp.get("data", [])
    if isinstance(data, list):
        for wf in data:
            if wf.get("name") == name:
                return wf
    # Also handle paginated response
    if isinstance(resp, dict) and "data" in resp:
        pass
    return None

def deploy_or_update(wf):
    name = wf["name"]
    existing = find_workflow_by_name(name)
    if existing:
        wid = existing["id"]
        # Update via PATCH (n8n v2 uses PATCH)
        print(f"  Updating existing workflow '{name}' ({wid})...")
        # n8n expects PUT to /rest/workflows/{id}
        r = subprocess.run(["curl","-s","-b",COOKIE_FILE,"-X","PATCH",f"{N8N_URL}/rest/workflows/{wid}",
            "-H","Content-Type: application/json","-H","X-CSRF-Token: cookie","-d",json.dumps(wf)],
            capture_output=True,text=True,timeout=30)
        try:
            resp = json.loads(r.stdout)
            if resp.get("data", {}).get("id") or resp.get("id"):
                vid = resp.get("data", {}).get("versionId", "") or resp.get("versionId", "")
                print(f"  Updated -> id={wid} versionId={vid}")
                return wid, vid
            # Fallback: try PUT
            if "error" in resp or "message" in resp:
                print(f"  PATCH response: {r.stdout[:500]}")
                print("  Trying PUT...")
                r2 = subprocess.run(["curl","-s","-b",COOKIE_FILE,"-X","PUT",f"{N8N_URL}/rest/workflows/{wid}",
                    "-H","Content-Type: application/json","-H","X-CSRF-Token: cookie","-d",json.dumps(wf)],
                    capture_output=True,text=True,timeout=30)
                try:
                    resp2 = json.loads(r2.stdout)
                    vid2 = resp2.get("data", {}).get("versionId", "") or resp2.get("versionId", "")
                    print(f"  PUT -> id={wid} versionId={vid2}")
                    return wid, vid2
                except:
                    print(f"  PUT raw: {r2.stdout[:500]}")
            print(f"  Update raw: {r.stdout[:500]}")
        except Exception as e:
            print(f"  Update parse error: {e} raw: {r.stdout[:500]}")
        return wid, ""
    else:
        print(f"  Creating workflow '{name}'...")
        r = subprocess.run(["curl","-s","-b",COOKIE_FILE,"-X","POST",f"{N8N_URL}/rest/workflows",
            "-H","Content-Type: application/json","-H","X-CSRF-Token: cookie","-d",json.dumps(wf)],
            capture_output=True,text=True,timeout=30)
        try:
            resp = json.loads(r.stdout)
            if "data" in resp and "id" in resp["data"]:
                print(f"  Created -> id={resp['data']['id']} name={resp['data']['name']}")
                return resp["data"]["id"], resp["data"].get("versionId","")
            print(f"  Create ERROR: {resp}")
        except:
            print(f"  Create PARSE ERROR: {r.stdout[:500]}")
        return None, ""

def activate(wfid, vid):
    if not wfid:
        return
    r = subprocess.run(["curl","-s","-b",COOKIE_FILE,"-X","POST",
        f"{N8N_URL}/rest/workflows/{wfid}/activate",
        "-H","Content-Type: application/json","-H","X-CSRF-Token: cookie",
        "-d",json.dumps({"versionId": vid})], capture_output=True,text=True,timeout=15)
    try:
        resp = json.loads(r.stdout)
        if resp.get("data",{}).get("active") == True:
            print(f"  Activated: {wfid}")
        else:
            print(f"  Activate response: {r.stdout[:500]}")
    except:
        print(f"  Activate raw: {r.stdout[:500]}")

# ---------------------------------------------------------------------------
# Build Track Engine workflow
# ---------------------------------------------------------------------------

def build_track_engine():
    # Positions layout (x,y)
    p_webhook = [250, 300]
    p_load_session = [450, 300]
    p_load_track = [650, 300]
    p_load_run = [650, 500]
    p_merge = [850, 400]
    p_get_step = [1050, 400]
    p_switch = [1250, 400]

    # Branch positions
    p_msg = [1450, 100]
    p_prepare = [1450, 250]
    p_jira_req = [1450, 400]
    p_validate = [1450, 550]
    p_condition = [1450, 700]
    p_action = [1450, 850]

    # LLM for input branch
    llm_chain_pos = [1650, 250]
    llm_model_pos = [1650, 400]
    llm_extract_pos = [1850, 250]
    p_extract_vars = [2050, 250]

    # Jira mock steps
    p_jira_mock = [1650, 400]  # after http request
    p_validate_jira = [1650, 400]  # reuse
    # Actually jira branch: HTTP Request -> Code JiraMock
    p_jira_http = [1450, 400]
    p_jira_code = [1650, 400]

    # Converge
    p_save_state = [2250, 400]
    p_determine = [2450, 400]
    p_save_run = [2650, 400]
    p_respond = [2850, 400]

    nodes = []
    # 1. Webhook
    webhook = webhook_node("Track Engine Webhook", p_webhook, path="track-engine", method="POST", response_mode="responseNode")
    # Remove auto-generated webhookId and ensure path is track-engine
    webhook["webhookId"] = "track-engine"
    webhook["parameters"]["path"] = "track-engine"
    nodes.append(webhook)

    nodes.append(code_node("Load Session", LOAD_SESSION_CODE, p_load_session))

    # File server read for track: path = tracks/{{team}}/{{track_id}}/track.json
    # Use HTTP GET with expression
    track_url = FS + "/read?path=/home/deb/n8n-track-platform/tracks/={{ $json.team }}/={{ $json.track_id }}/track.json"
    # n8n expression: need to use =$json.team
    # We'll build URL with n8n expression syntax
    track_get = http_get_node("Load Track", "={{ 'http://172.20.0.1:19099/read?path=/home/deb/n8n-track-platform/tracks/' + $json.team + '/' + $json.track_id + '/track.json' }}", p_load_track)
    nodes.append(track_get)

    # Backend run load: if run_id present, GET /api/runs/{run_id}, else stub
    run_get = http_get_node("Load Run", "={{ $json.run_id ? 'http://172.20.0.1:19001/api/runs/' + $json.run_id : 'http://172.20.0.1:19001/api/health' }}", p_load_run)
    nodes.append(run_get)

    nodes.append(merge_node("Merge Track+Run", p_merge))
    nodes.append(code_node("Get Current Step", GET_CURRENT_STEP_CODE, p_get_step))

    # Switch node: 6 outputs (message, input, jira, validation, condition, action-default)
    switch_rules = [
        {"operation": "equal", "value1": "={{ $json.step_type }}", "value2": "message"},
        {"operation": "isTrue", "value1": "={{ ['input','question','approval'].includes($json.step_type) }}"},
        {"operation": "equal", "value1": "={{ $json.step_type }}", "value2": "jira"},
        {"operation": "equal", "value1": "={{ $json.step_type }}", "value2": "validation"},
        {"operation": "equal", "value1": "={{ $json.step_type }}", "value2": "condition"},
    ]
    sw = switch_node("Route by Type", p_switch, switch_rules)
    nodes.append(sw)

    # Branch 0: message
    nodes.append(code_node("Handle Message", HANDLE_MESSAGE_CODE, p_msg))

    # Branch 1: input/question/approval -> LLM flow
    nodes.append(code_node("Prepare LLM Input", PREPARE_LLM_INPUT_CODE, p_prepare))
    # llm_pair for intent
    llm_nodes = llm_pair("Intent Extractor", "publisher", llm_chain_pos, llm_model_pos, max_tokens=2048, temperature=0.2)
    nodes.extend(llm_nodes)
    nodes.append(code_node("Extract Variables", EXTRACT_VARIABLES_CODE, p_extract_vars))

    # Branch 2: jira
    # Mock Jira HTTP — use jsonplaceholder or httpbin as mock, but handle failure gracefully
    jira_http = http_get_node("Fetch Jira", "={{ 'https://jsonplaceholder.typicode.com/posts/1' }}", p_jira_http)
    # Ensure it doesn't fail workflow on 404
    jira_http["parameters"]["options"] = {"redirect": {"redirect": "follow"}, "timeout": 5000}
    jira_http["parameters"]["alwaysOutputData"] = True
    jira_http["continueOnFail"] = True
    nodes.append(jira_http)
    nodes.append(code_node("Handle Jira", JIRA_MOCK_CODE, p_jira_code))

    # Branch 3: validation
    nodes.append(code_node("Validate Step", VALIDATE_CODE, p_validate))

    # Branch 4: condition
    nodes.append(code_node("Evaluate Condition", CONDITION_CODE, p_condition))

    # Branch 5: action/llm/wait/webhook (default)
    nodes.append(code_node("Execute Action", ACTION_STUB_CODE, p_action))

    # Converge nodes
    nodes.append(code_node("Save State", SAVE_STATE_CODE, p_save_state))
    nodes.append(code_node("Determine Transition", DETERMINE_TRANSITION_CODE, p_determine))

    # Save run: POST to backend /api/runs/{run_id}/advance or file server write
    # Use file server write for durability: POST /write?path=/.../runs/{team}/{run_id}.json  with body = updatedRun
    save_run = http_post_node("Save Run",
        "={{ 'http://172.20.0.1:19099/write?path=/home/deb/n8n-track-platform/runs/' + $json.team + '/' + $json.run_id + '.json' }}",
        p_save_run, json_body="={{ $json.updatedRun }}")
    nodes.append(save_run)

    # Also optionally call backend advance for live runs (best effort, continueOnFail)
    # We'll keep single save via file server to avoid duplication

    nodes.append(respond_node("Respond", p_respond, respond_with="json", response_data="={{ { run_id: $json.run_id, team: $json.team, track_id: $json.track_id, next_step_id: $json.next_step_id, next_text: $json.next_text, status: $json.status, output_text: $json.output_text, variables: $json.variables, matched_transition: $json.matched_transition } }}"))

    # Connections
    conns = {
        "Track Engine Webhook": {"main": [[{"node": "Load Session", "type": "main", "index": 0}]]},
        "Load Session": {"main": [[{"node": "Load Track", "type": "main", "index": 0}, {"node": "Load Run", "type": "main", "index": 0}]]},
        "Load Track": {"main": [[{"node": "Merge Track+Run", "type": "main", "index": 0}]]},
        "Load Run": {"main": [[{"node": "Merge Track+Run", "type": "main", "index": 1}]]},
        "Merge Track+Run": {"main": [[{"node": "Get Current Step", "type": "main", "index": 0}]]},
        "Get Current Step": {"main": [[{"node": "Route by Type", "type": "main", "index": 0}]]},
        # Switch outputs: 0=message,1=input,2=jira,3=validation,4=condition,5=fallback(action)
        "Route by Type": {"main": [
            [{"node": "Handle Message", "type": "main", "index": 0}],
            [{"node": "Prepare LLM Input", "type": "main", "index": 0}],
            [{"node": "Fetch Jira", "type": "main", "index": 0}],
            [{"node": "Validate Step", "type": "main", "index": 0}],
            [{"node": "Evaluate Condition", "type": "main", "index": 0}],
            [{"node": "Execute Action", "type": "main", "index": 0}],
        ]},
        "Handle Message": {"main": [[{"node": "Save State", "type": "main", "index": 0}]]},
        "Prepare LLM Input": {"main": [[{"node": "Intent Extractor", "type": "main", "index": 0}]]},
        "Intent Extractor Model": {"ai_languageModel": [[{"node": "Intent Extractor", "type": "ai_languageModel", "index": 0}]]},
        "Intent Extractor": {"main": [[{"node": "Extract Intent Extractor", "type": "main", "index": 0}]]},
        "Extract Intent Extractor": {"main": [[{"node": "Extract Variables", "type": "main", "index": 0}]]},
        "Extract Variables": {"main": [[{"node": "Save State", "type": "main", "index": 0}]]},
        "Fetch Jira": {"main": [[{"node": "Handle Jira", "type": "main", "index": 0}]]},
        "Handle Jira": {"main": [[{"node": "Save State", "type": "main", "index": 0}]]},
        "Validate Step": {"main": [[{"node": "Save State", "type": "main", "index": 0}]]},
        "Evaluate Condition": {"main": [[{"node": "Save State", "type": "main", "index": 0}]]},
        "Execute Action": {"main": [[{"node": "Save State", "type": "main", "index": 0}]]},
        "Save State": {"main": [[{"node": "Determine Transition", "type": "main", "index": 0}]]},
        "Determine Transition": {"main": [[{"node": "Save Run", "type": "main", "index": 0}]]},
        "Save Run": {"main": [[{"node": "Respond", "type": "main", "index": 0}]]},
    }
    return make_wf("Track Engine", nodes, conns)

def build_event_logger():
    p_webhook = [250, 300]
    p_code = [450, 300]
    p_respond = [650, 300]
    nodes = [
        webhook_node("Track Event Webhook", p_webhook, path="track-events", method="POST", response_mode="responseNode"),
        code_node("Log Event", EVENT_LOGGER_CODE, p_code),
        respond_node("Respond", p_respond, respond_with="json", response_data="={{ $json }}"),
    ]
    # Fix webhook path
    nodes[0]["webhookId"] = "track-events"
    nodes[0]["parameters"]["path"] = "track-events"
    conns = {
        "Track Event Webhook": {"main": [[{"node": "Log Event", "type": "main", "index": 0}]]},
        "Log Event": {"main": [[{"node": "Respond", "type": "main", "index": 0}]]},
    }
    return make_wf("Track Event Logger", nodes, conns)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=== Track Platform Deploy ===")
    print(f"n8n URL: {N8N_URL}")
    print(f"Mistral credential: {MISTRAL_CREDENTIAL_ID}")

    # Start file server as fallback
    try:
        start_file_server()
    except Exception as e:
        print(f"  File server start note: {e}")

    # Check n8n reachable
    try:
        r = subprocess.run(["curl","-s",f"{N8N_URL}/rest/workflows", "-b", COOKIE_FILE, "-H","X-CSRF-Token: cookie"],
            capture_output=True,text=True,timeout=10)
        if "data" not in r.stdout and "workflows" not in r.stdout.lower():
            print(f"  Warning: n8n not reachable or auth missing: {r.stdout[:400]}")
            print("  Ensure n8n is running on 5679 and /tmp/n8n_cookies.txt is valid (login via browser or n8n API).")
        else:
            print("  n8n reachable")
    except Exception as e:
        print(f"  n8n check failed: {e}")

    # Deploy Track Engine
    print("\n[1/2] Track Engine...")
    wf1 = build_track_engine()
    wfid1, vid1 = deploy_or_update(wf1)
    if wfid1:
        activate(wfid1, vid1)

    print("\n[2/2] Track Event Logger...")
    wf2 = build_event_logger()
    wfid2, vid2 = deploy_or_update(wf2)
    if wfid2:
        activate(wfid2, vid2)

    print("\nDone! Workflows deployed (create/update, no deletions).")
    print(f"  Track Engine webhook: POST {N8N_URL}/webhook/track-engine")
    print(f"  Event Logger webhook: POST {N8N_URL}/webhook/track-events")
    print(f"  File server: http://localhost:{FILE_SERVER_PORT}  (GET /read?path=, POST /write?path=, GET /list?path=)")
    print(f"  Backend:     http://localhost:{BACKEND_PORT}  (GET /api/tracks, POST /api/runs/start, etc.)")
