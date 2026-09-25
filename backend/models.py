"""Pydantic models: tracks (what the analyst builds), runs (a user's pass through a track),
settings (what the analyst configures)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator

SLUG_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(v: str) -> str:
    if not v or not SLUG_RE.match(v):
        raise ValueError("must match ^[a-zA-Z0-9_-]+$")
    return v


# ---------------------------------------------------------------------------
# Track — stored in the partner system's format:
#   scheme.bpmn, tasks.json, Activity_*/{metadata,properties,attachments}.json (kept verbatim)
# plus our sidecars that the partner system ignores: track.json and Activity_*/agent.json
# ---------------------------------------------------------------------------

class TrackStatus(str, Enum):
    draft = "draft"
    published = "published"
    archived = "archived"


class Link(BaseModel):
    title: str = ""
    url: str
    # who the document is for: the agent reads all of them, "user"/"both" are also shown to the user
    audience: Literal["agent", "user", "both"] = "both"

    @field_validator("url")
    @classmethod
    def http_only(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError("link url must start with http:// or https://")
        return v


class Requirement(BaseModel):
    """A mandatory condition: the step cannot be left until the user confirms it."""
    id: str
    text: str

    @field_validator("id")
    @classmethod
    def _slug_id(cls, v: str) -> str:
        return _slug(v)


class JiraTemplate(BaseModel):
    """Create a Jira issue on this step. `{codeName}` placeholders are filled from properties."""
    project: str = ""
    issue_type: str = "Task"
    summary: str = ""
    description: str = ""
    labels: List[str] = Field(default_factory=list)
    priority: str = ""
    required_fields: List[str] = Field(default_factory=list)
    required: bool = True  # step cannot be left before the issue is created
    extra_fields: Dict[str, Any] = Field(default_factory=dict)

    def placeholders(self) -> List[str]:
        keys = re.findall(r"\{([a-zA-Z0-9_-]+)\}", self.summary + " " + self.description)
        out: List[str] = []
        for k in list(self.required_fields) + keys:
            if k not in out:
                out.append(k)
        return out


class AgentExtras(BaseModel):
    """Activity_*/agent.json — what the agent needs beyond the partner format."""
    agent_instructions: str = ""
    requirements: List[Requirement] = Field(default_factory=list)
    jira: Optional[JiraTemplate] = None
    link_audience: Dict[str, Literal["agent", "user", "both"]] = Field(default_factory=dict)
    extra_links: List[Link] = Field(default_factory=list)  # documents not linked from content


class Activity(BaseModel):
    metadata: Dict[str, Any] = Field(default_factory=dict)
    properties: List[Dict[str, Any]] = Field(default_factory=list)
    attachments: List[Any] = Field(default_factory=list)
    agent: AgentExtras = Field(default_factory=AgentExtras)

    @property
    def name(self) -> str:
        return str(self.metadata.get("name") or "")

    @property
    def content(self) -> str:
        return str(self.metadata.get("content") or "")


def new_activity_metadata(task_id: str, name: str, order: int, task_type: str = "task") -> Dict[str, Any]:
    """Same keys and defaults as metadata.json exported by the partner system."""
    return {"id": str(uuid.uuid4()), "name": name, "codeName": task_id, "content": "",
            "format": "tiptap", "displayOrder": order, "type": task_type, "jiraSendTask": None,
            "sberTrackSendTask": None, "waitTask": None,
            "isTaskCompletionNotificationActive": False, "properties": [], "attachments": [],
            "commentsCount": 0, "timeEstimate": {"quantity": 0, "unit": "minutes"},
            "isNew": False, "isModified": False, "isDeleted": False, "error": None}


def new_property(track_uuid: str, code: str = "", name: str = "") -> Dict[str, Any]:
    return {"id": str(uuid.uuid4()), "name": name, "codeName": code, "description": "",
            "isArtifact": False, "isEditableInTaskOnly": True, "requiredToFillOut": True,
            "valueType": "string", "valueVariants": [], "trackId": track_uuid, "isUnused": False}


class TrackMeta(BaseModel):
    """track.json — our sidecar with platform-only data."""
    id: str
    name: str = Field(..., min_length=1)
    description: str = ""
    status: TrackStatus = TrackStatus.draft
    version: int = Field(default=1, ge=1)
    track_uuid: str = Field(default_factory=lambda: str(uuid.uuid4()))  # properties[].trackId
    agent_instructions: str = ""  # applies to every step
    model: str = ""  # overrides the global model when set
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @field_validator("id")
    @classmethod
    def _slug_id(cls, v: str) -> str:
        return _slug(v)


class Track(TrackMeta):
    bpmn: str
    tasks: List[str] = Field(default_factory=list)  # tasks.json
    activities: Dict[str, Activity] = Field(default_factory=dict)
    _graph: Any = PrivateAttr(default=None)

    @model_validator(mode="after")
    def parse_scheme(self) -> "Track":
        from backend import bpmn  # local import: bpmn has no model dependencies
        try:
            self._graph = bpmn.parse(self.bpmn)
        except bpmn.BpmnError as e:
            raise ValueError(str(e)) from e
        return self

    @property
    def graph(self):
        return self._graph

    def activity(self, task_id: Optional[str]) -> Optional[Activity]:
        return self.activities.get(task_id or "")

    def step_name(self, task_id: Optional[str]) -> str:
        node = self.graph.nodes.get(task_id or "")
        act = self.activity(task_id)
        return " ".join(((node.name if node else "") or (act.name if act else "") or (task_id or "")).split())

    def props(self) -> Dict[str, Dict[str, Any]]:
        """codeName -> property (with "_activity" = owning task id)."""
        out: Dict[str, Dict[str, Any]] = {}
        for tid in self.tasks or list(self.activities):
            act = self.activities.get(tid)
            for p in act.properties if act else []:
                code = p.get("codeName")
                if code and code not in out:
                    out[code] = {**p, "_activity": tid}
        return out

    def meta(self) -> TrackMeta:
        return TrackMeta(**self.model_dump(include=set(TrackMeta.model_fields)))

    def touch(self) -> None:
        now = now_iso()
        self.created_at = self.created_at or now
        self.updated_at = now


class TrackIn(BaseModel):
    """Body of POST/PUT /api/tracks. An empty `bpmn` on create means "start from a template"."""
    id: str
    name: str
    description: str = ""
    agent_instructions: str = ""
    model: str = ""
    bpmn: str = ""
    tasks: List[str] = Field(default_factory=list)
    activities: Dict[str, Activity] = Field(default_factory=dict)
    task_types: Dict[str, str] = Field(default_factory=dict)  # editor: task id -> task|jira-send


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

class RunStatus(str, Enum):
    active = "active"
    completed = "completed"
    cancelled = "cancelled"


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "event"]
    text: str
    step_id: Optional[str] = None
    at: str = Field(default_factory=now_iso)


class StepVisit(BaseModel):
    step_id: str
    entered_at: str = Field(default_factory=now_iso)
    left_at: Optional[str] = None


class JiraIssueRef(BaseModel):
    key: str
    url: str = ""
    summary: str = ""
    step_id: Optional[str] = None
    status: str = ""
    created_at: str = Field(default_factory=now_iso)


class Run(BaseModel):
    run_id: str
    user_id: str = Field(..., min_length=1)
    user_name: str = ""
    track_id: str
    track_version: int = Field(..., ge=1)
    track_name: str = ""
    status: RunStatus = RunStatus.active
    current_step: Optional[str] = None
    facts: Dict[str, Any] = Field(default_factory=dict)  # everything the user told us
    confirmed: Dict[str, Dict[str, str]] = Field(default_factory=dict)  # step -> req id -> evidence
    jira_issues: List[JiraIssueRef] = Field(default_factory=list)
    visits: List[StepVisit] = Field(default_factory=list)
    messages: List[ChatMessage] = Field(default_factory=list)
    summary: str = ""
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)
    finished_at: Optional[str] = None

    def touch(self) -> None:
        self.updated_at = now_iso()


class StartRunIn(BaseModel):
    track_id: str
    user_id: str = Field(..., min_length=1, max_length=128)
    user_name: str = ""


class MessageIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=20000)


# ---------------------------------------------------------------------------
# Settings (config/settings.json)
# ---------------------------------------------------------------------------

class AgentSettings(BaseModel):
    base_url: str = "https://api.mistral.ai/v1"  # any OpenAI-compatible /chat/completions
    model: str = "mistral-medium-latest"
    api_key: str = ""
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1500, ge=64)
    system_prompt: str = ""  # appended to the built-in agent prompt
    history_limit: int = Field(default=40, ge=4)  # chat messages sent to the model
    timeout_s: int = Field(default=90, ge=5)
    extra_body: Dict[str, Any] = Field(default_factory=dict)  # merged into every request


class JiraSettings(BaseModel):
    mode: Literal["mock", "cloud", "server"] = "mock"
    base_url: str = ""
    email: str = ""  # cloud: basic auth email
    token: str = ""  # cloud: API token, server: personal access token
    default_project: str = ""


class LinkSettings(BaseModel):
    confluence_base_url: str = ""
    confluence_email: str = ""
    confluence_token: str = ""
    timeout_s: int = Field(default=15, ge=2)
    max_chars: int = Field(default=12000, ge=1000)  # per link, injected into the agent context


class RunSettings(BaseModel):
    stale_hours: int = Field(default=24, ge=1)  # an active run with no activity is "stalled"


class Settings(BaseModel):
    agent: AgentSettings = Field(default_factory=AgentSettings)
    jira: JiraSettings = Field(default_factory=JiraSettings)
    links: LinkSettings = Field(default_factory=LinkSettings)
    runs: RunSettings = Field(default_factory=RunSettings)


SECRET_FIELDS = {("agent", "api_key"), ("jira", "token"), ("links", "confluence_token")}
