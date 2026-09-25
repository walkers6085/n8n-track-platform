# AGENTS — Track Platform

## Build & Run

```bash
pip install -r backend/requirements.txt
uvicorn backend.main:app --host 0.0.0.0 --port 19001 --reload
docker compose up -d
python3 -m unittest discover -s tests -t . -v
```

No frontend build step: static files in `frontend/` are served by the backend.

## Conventions

- **Models:** `backend/models.py` is the single source of truth (Pydantic v2). Import enums and `FINISH` from there instead of redefining them.
- **Track format = the partner system's format.** `tracks/<id>/scheme.bpmn`, `tasks.json`, `Activity_*/{metadata,properties,attachments}.json` must stay byte-compatible: never rewrite keys that are present (`tracks.sync` only fills missing ones, appended at the end), write partner JSON compact (`storage._compact`), keep unknown fields. Our data goes only into the sidecars `track.json` and `Activity_*/agent.json`.
- **Storage:** filesystem JSON only (`backend/storage.py`). The data root is the repo root, or `TRACK_PLATFORM_DATA`. Tests call `storage.set_root(tmp)`. Full snapshots are in `track_versions/<id>/v<N>/`. The version goes up only when content changes.
- **The scheme decides the flow (`bpmn.py`).** Conditions are `${objProps.prop("code").value() == value}`. `bpmn.follow` computes the next step from run facts, and the model never picks branches. Leaving a step (`engine.complete`) requires the step's required properties, confirmed requirements (agent.json), an issue on `jira-send` steps, and the values needed by the next gateway. `go_back` is allowed only to visited steps. Only published tracks start runs.
- **Agent:** `agent.py` uses OpenAI-compatible tool calling via `llm.chat`. The system prompt is rebuilt every round from run state, so facts are never lost to history truncation. `read_link` may fetch only URLs that are present in step content or extra links (SSRF guard). Values go through `engine.coerce`: variants, booleans, numbers and links.
- **API:** user routes (`/api/catalog`, `/api/runs…`) are open. Analyst routes require `X-Analyst-Token` when `ANALYST_TOKEN` is set. Secrets in settings are masked on GET, and a masked or empty value on PUT keeps the stored one.
- **Frontend:** vanilla JS, no bundler. `common.js` holds shared helpers (`api`, `h`, `md`, `safeHtml`, formatters), `app.js` is the user UI, `analyst.js` holds the analyst pages, and `editor.js` is the BPMN editor. bpmn-js is vendored in `frontend/vendor/bpmn-js` (do not remove the bpmn.io watermark, which the license requires). The API is same-origin (`/api`). Keep strings in Russian.
- **Tests:** `unittest`, no network. The LLM is replaced by `ScriptedLLM` (passed as `chat=` or patched into `agent.llm.chat`). Build test tracks with `tracks.from_spec`. `tests/fixtures/partner/` is a pristine copy of the partner example (its scheme had 3 copy-paste breaks, repaired). It is gitignored because it contains internal links and names; tests that need it are skipped when it is absent.
- **Formatting:** Python 3.11+, sorted imports, 100-char lines.
- **Port:** 19001.
