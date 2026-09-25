# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Read `AGENTS.md` for conventions and `README.md` for the product description (in Russian). This file covers how the pieces fit together.

## Commands

```bash
pip install -r backend/requirements.txt
MISTRAL_API_KEY=... uvicorn backend.main:app --port 19001 --reload     # UI: / and /analyst.html

python3 -m unittest discover -s tests -t . -v                            # all tests (no network)
python3 -m unittest tests.test_tracks.TestAgent -v                       # one class
python3 -m unittest tests.test_tracks.TestAgent.test_jira_template_asks_then_creates -v
```

Run commands from the repo root, because modules import as `backend.*`. There is no linter and no frontend build.

## Architecture

- **Track = partner system format.** Read `AGENTS.md` → "Track format". `models.Track` is the track meta (`track.json`), plus `bpmn` (the scheme text), `tasks` and `activities` (raw partner dicts plus `AgentExtras`). `Track.graph` is the parsed scheme (`bpmn.Graph`: nodes, flows with parsed conditions, groups by geometry, shape bounds).
- **Consistency:** `tracks.sync` runs on every save and import. It creates activities for new tasks, drops activities of removed ones, turns plain `bpmn:task` into `serviceTask` with `camunda:type/topic` and `taskType` (`bpmn.normalize` returns the input unchanged when it already conforms), and fills missing partner keys. `tracks.lint` = `bpmn.lint` plus activity checks. Errors block publishing.
- **Request flow for a chat message:** `main.post_message` takes the per-run lock and calls `agent.run_turn`. `run_turn` loops up to `MAX_ROUNDS`. Each round rebuilds the system prompt (`agent.system_prompt`: collected facts with property labels, plus the current step brief with content-as-text, properties, upcoming gateway decisions, gates status and prefetched link text) and calls `llm.chat` with `TOOLS`. Tools mutate the `Run` through `engine`. `complete_step` → `engine.complete` → `bpmn.follow`.
- **Safety nets around a weak model** (all tested):
  - `normalize_keys` maps invented keys to codeNames.
  - `engine.coerce` rejects values outside variants or type, with a hint.
  - The `save_info` result lists what is still missing.
  - `NUDGE_PROMPT` re-prompts once per turn when the step became ready but was not completed.
  - `link_jira_issue` covers the case when Jira is unreachable.
- **Model config:** z.ai `glm-4.5-flash` (`https://api.z.ai/api/paas/v4`) in `config/settings.json`, with `agent.extra_body = {"thinking": {"type": "disabled"}}`. `extra_body` is merged into every request, and `llm.chat` retries on 429, 5xx and dropped connections.
- **Runs pin `track_version`.** `engine.track_for_run` loads `track_versions/<id>/v<N>/`. Progress percent = done / (done + 1 + `bpmn.remaining`).
- **Editor (`frontend/editor.js`):** a bpmn-js Modeler plus a side panel keyed by selection. Activity data lives in `ed.activities`. Task type changes go to `ed.taskTypes`, and the server writes them into the scheme. Flow conditions are written as `bpmn:FormalExpression`. On save the server may normalize the XML, so the editor re-imports it and keeps the viewbox and selection. The bpmn-js round-trip preserves camunda attributes, bioc colours and groups (verified on the partner scheme, which is kept local only).
- **Import/export:** `POST /api/tracks/import` takes a raw zip body (no multipart). `GET /api/tracks/<id>/export?pure=true` leaves out our sidecars. Analyst auth also accepts `?token=` for download links.
- **Links:** `links.extract_links` skips people mentions (`confluence-userlink`, `/display/~`). `links.fetch_text` handles HTML and the Confluence REST API, and caches for 10 minutes.
- **Jira:** `jira.py` supports `mock` (`config/jira_mock.json`), `cloud` and `server` via REST v2. For `jira-send` steps without our template, `metadata.jiraSendTask` is passed to the agent as a hint; its real structure is still unknown.
