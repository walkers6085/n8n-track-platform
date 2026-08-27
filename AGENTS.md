# AGENTS — n8n Track Platform

## Build & Run

```bash
pip install -r backend/requirements.txt
uvicorn backend.main:app --host 0.0.0.0 --port 19001 --reload
python3 files/file_server.py   # :19099
docker compose up -d backend file-server
docker compose --profile isolated up -d
python3 -m unittest tests.test_tracks -v
```

No frontend build step — static files in `frontend/`.

## Conventions

- **Models:** `backend/models.py` is single source of truth (Pydantic). Do not duplicate enums; import `TrackStatus`, `RunStatus`, `StepType`.
- **Storage:** filesystem JSON only (`backend/storage.py`). Tracks: `tracks/{team}/{id}/track.json` + `v{N}.json`. Runs: `runs/{team}/{run_id}.json`. Version auto-increments only when `model_dump(exclude={created_at,updated_at})` differs.
- **API:** FastAPI in `backend/main.py`, CORS `*`, mount `frontend/` at `/`. All routes under `/api`.
- **Frontend:** vanilla JS + `fetch`, API base `http://localhost:19001`. No npm, no bundler. Keep `index.html` + `app.js` + `style.css` small and readable.
- **LLM inheritance:** Global → Team → Track → Step. Implement merge as “last non-null wins”. Store global in `config/llm.json`.
- **Business rules:**
  - Published track cannot be deleted (client + server guard; archive first).
  - Run pins `track_version` at start; later track edits don’t affect running runs (load `version=track_version` then fallback to latest).
  - Transitions with empty `condition` are unconditional; unconditional cycles are forbidden.
  - Archived tracks cannot start new runs.
- **Validation:** slug regex `^[a-zA-Z0-9_-]+$`; step ids unique per track; transitions `from/to` must exist; `StepType` enum closed.
- **Tests:** `tests/test_tracks.py` — `unittest`, no external deps. Cover schema, transitions, types, versioning, run states, LLM inheritance, business rules. Test against real `tracks/` fixtures and temp `storage` dirs.
- **Formatting:** Python 3.11+, FastAPI/Pydantic v2. No linter enforced; keep imports sorted, 100-char lines.
- **Ports:** 19001 backend, 19099 file-server, 5680 isolated n8n.

## Key Files

| Path | Role |
|---|---|
| `backend/models.py` | Pydantic models, enums, validators |
| `backend/storage.py` | Filesystem CRUD |
| `backend/main.py` | FastAPI routes, `_next_step`, run lifecycle |
| `frontend/index.html` | SPA layout (Run + Admin tabs) |
| `frontend/app.js` | Fetch logic, chat flow, admin graph |
| `frontend/style.css` | Clean responsive CSS |
| `files/file_server.py` | HTTP file server :19099 (/read,/write,/list) |
| `tracks/` | Track snapshots |
| `runs/` | Run histories |
| `config/` | llm.json, teams.json |

## n8n Integration

n8n workflows call `POST /api/runs/{id}/advance` with `{target_step, variables, status, text}` to drive runs. Use file-server `http://file-server:19099/read?path=` inside n8n HTTP Request nodes if needed.

## Pitfalls

- `storage.py` path traversal guard: reject `..` and `/` in `run_id`.
- `create_or_update_track` merges existing fields when caller omits them; empty list means “keep existing”.
- Frontend graph is visual only; positions from `step.position {x,y}` or auto-layout.

