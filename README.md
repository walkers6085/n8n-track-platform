# n8n Track Platform

Track-based orchestration for n8n: define multi-step workflows (tracks) per team, run them via chat/API, drive execution with n8n webhooks.

## Architecture

```
                ┌─────────────────┐
                │   Frontend SPA   │  :19001 /  (static)
                │ index.html+js+css│
                └────────┬────────┘
                         │ fetch
                ┌────────▼────────┐
                │  Backend (FastAPI) │  :19001 /api
                │  backend/main.py   │──► tracks/{team}/{id}/track.json + v{N}.json
                │  backend/storage.py│──► runs/{team}/{run_id}.json
                └────────┬────────┘  └──► config/llm.json, config/teams.json
                         │ webhook
                ┌────────▼────────┐
                │   n8n workflows  │  POST /api/runs/{id}/advance
                │   (triggered)    │
                └─────────────────┘
                ┌─────────────────┐
                │ File Server :19099│  GET /read?path=  POST /write?path=
                └─────────────────┘
```

**State:** filesystem JSON only (no DB). `Track.version` is auto-incremented on content change; each save writes `v{N}.json`. `Run.track_version` is snapshotted at start and pinned to that version.

## Quick Start

```bash
pip install -r backend/requirements.txt
uvicorn backend.main:app --host 0.0.0.0 --port 19001 --reload
# file server (for n8n HTTP Request nodes)
python3 files/file_server.py  # :19099
# docker (backend + file-server + optional n8n)
docker compose up -d backend file-server
docker compose --profile isolated up -d  # + n8n on :5680

# deploy n8n workflows (if workflows/deploy.py present)
python workflows/deploy.py
```

Open http://localhost:19001 — frontend served by backend StaticFiles mount.

## API Docs

Interactive docs at `/docs` (FastAPI Swagger). Base URL `http://localhost:19001`.

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | Health check |
| POST | `/api/tracks` | Create/update track (body: `CreateTrackRequest`) |
| GET | `/api/tracks?team=` | List tracks (optional filter) |
| GET | `/api/tracks/{team}/{id}?version=` | Get track (latest or versioned) |
| GET | `/api/tracks/{team}/{id}/versions` | List versions |
| POST | `/api/tracks/{team}/{id}/publish` | Publish (sets status=published, bumps version file) |
| DELETE | `/api/tracks/{team}/{id}` | Delete track directory |
| POST | `/api/runs/start` | Start run `{track_id, team, user_id, variables?}` |
| GET | `/api/runs?team=&user_id=` | List runs |
| GET | `/api/runs/{id}?team=` | Get run |
| POST | `/api/runs/{id}/message` | User message `{text, user_id?, payload?}` — appends history, auto-advances via transitions |
| POST | `/api/runs/{id}/advance` | n8n webhook `{target_step?, variables?, status?, text?}` |
| GET | `/api/teams` | List teams (from config + discovered) |
| GET | `/api/config/llm` | Get global LLM config |
| PUT | `/api/config/llm` | Save global LLM config |

**LLM inheritance:** `Global (config/llm.json) → Team → Track.llm_config → Step.config.llm` — later overrides former, non-null fields win.

**Run history types:** `message | system | step_enter | step_exit`.

## Frontend

`frontend/` — vanilla JS SPA (no build):
- **Left:** track list grouped by team, filter.
- **Center:** chat — select track → Start run → first step message → input → POST message → bot reply; progress updates.
- **Right:** step list with `completed/current/pending` dots + variables + mini history.
- **Admin tab:** Tracks editor (list, create, edit JSON, SVG graph, step cards, transition editor), Publish button, LLM config editor.

API base is `http://localhost:19001`.

## Tests

```bash
python3 -m unittest tests.test_tracks -v
```

40+ tests: schema, transitions, step types, versioning, run states, LLM inheritance, business rules.

## Folder Structure

```
backend/         FastAPI + storage.py + models.py (Pydantic)
frontend/        index.html, app.js, style.css
files/           file_server.py :19099
tracks/{team}/{id}/  track.json + vN.json
runs/{team}/     {run_id}.json
config/          llm.json, teams.json
workflows/       n8n workflow definitions
tests/           test_tracks.py
docker-compose.yml
```
