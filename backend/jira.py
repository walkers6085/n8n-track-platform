"""Jira client. "mock" mode keeps issues in config/jira_mock.json so tracks work without Jira."""

from __future__ import annotations

from typing import Any, Dict, List

import httpx

from backend import storage
from backend.models import JiraSettings, now_iso

MOCK_FILE = "jira_mock.json"


class JiraError(Exception):
    pass


def _client(cfg: JiraSettings) -> httpx.Client:
    if not cfg.base_url:
        raise JiraError("Jira не настроена: укажите адрес в настройках аналитика")
    kw: Dict[str, Any] = {"base_url": cfg.base_url.rstrip("/"), "timeout": 20,
                          "headers": {"Accept": "application/json"}}
    if cfg.mode == "cloud":
        kw["auth"] = (cfg.email, cfg.token)
    elif cfg.token:
        kw["headers"]["Authorization"] = f"Bearer {cfg.token}"
    return httpx.Client(**kw)


def _raise(r: httpx.Response) -> None:
    if r.status_code >= 400:
        try:
            data = r.json()
            msg = "; ".join(data.get("errorMessages", []) +
                            [f"{k}: {v}" for k, v in data.get("errors", {}).items()])
        except Exception:
            msg = r.text[:300]
        raise JiraError(f"Jira {r.status_code}: {msg or r.reason_phrase}")


def browse_url(cfg: JiraSettings, key: str) -> str:
    if cfg.mode == "mock":
        return ""
    return f"{cfg.base_url.rstrip('/')}/browse/{key}"


def create_issue(cfg: JiraSettings, project: str, issue_type: str, summary: str,
                 description: str, labels: List[str], priority: str,
                 extra_fields: Dict[str, Any]) -> Dict[str, str]:
    project = project or cfg.default_project
    if not project:
        raise JiraError("не указан проект Jira (ни в шаблоне шага, ни в настройках)")
    if cfg.mode == "mock":
        db = storage.load_json(MOCK_FILE, {"counters": {}, "issues": {}})
        n = db["counters"].get(project, 0) + 1
        db["counters"][project] = n
        key = f"{project}-{n}"
        db["issues"][key] = {"key": key, "summary": summary, "description": description,
                             "issue_type": issue_type, "labels": labels, "priority": priority,
                             "status": "Открыта", "created_at": now_iso()}
        storage.save_json(MOCK_FILE, db)
        return {"key": key, "url": ""}
    fields: Dict[str, Any] = {"project": {"key": project}, "summary": summary[:250],
                              "description": description, "issuetype": {"name": issue_type}}
    if labels:
        fields["labels"] = labels
    if priority:
        fields["priority"] = {"name": priority}
    fields.update(extra_fields or {})
    with _client(cfg) as c:
        r = c.post("/rest/api/2/issue", json={"fields": fields})
        _raise(r)
        key = r.json()["key"]
    return {"key": key, "url": browse_url(cfg, key)}


def issue_status(cfg: JiraSettings, key: str) -> Dict[str, str]:
    if cfg.mode == "mock":
        db = storage.load_json(MOCK_FILE, {"issues": {}})
        issue = db["issues"].get(key)
        if not issue:
            raise JiraError(f"задача {key} не найдена")
        return {"key": key, "status": issue["status"], "summary": issue["summary"],
                "assignee": "", "url": ""}
    with _client(cfg) as c:
        r = c.get(f"/rest/api/2/issue/{key}", params={"fields": "status,summary,assignee"})
        _raise(r)
        f = r.json()["fields"]
    return {"key": key, "status": f["status"]["name"], "summary": f.get("summary", ""),
            "assignee": (f.get("assignee") or {}).get("displayName", ""),
            "url": browse_url(cfg, key)}


def check_connection(cfg: JiraSettings) -> str:
    if cfg.mode == "mock":
        return "тестовый режим: задачи создаются локально"
    with _client(cfg) as c:
        r = c.get("/rest/api/2/myself")
        _raise(r)
        return f"подключено как {r.json().get('displayName', '?')}"
