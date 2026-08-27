"""Pydantic models for n8n Track Platform."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TrackStatus(str, Enum):
    draft = "draft"
    published = "published"
    archived = "archived"


class RunStatus(str, Enum):
    active = "active"
    waiting = "waiting"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class StepType(str, Enum):
    message = "message"
    input = "input"
    question = "question"
    jira = "jira"
    validation = "validation"
    action = "action"
    condition = "condition"
    approval = "approval"
    llm = "llm"
    webhook = "webhook"
    wait = "wait"


# ---------------------------------------------------------------------------
# Step / Transition / Track
# ---------------------------------------------------------------------------

STEP_TYPES = {e.value for e in StepType}

_SLUG_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


class Step(BaseModel):
    id: str = Field(..., description="Unique step identifier within the track")
    type: StepType = Field(..., description="Step type")
    name: Optional[str] = None
    config: Dict[str, Any] = Field(default_factory=dict)
    position: Optional[Dict[str, float]] = None  # {x, y} for frontend canvas

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        if not v or not _SLUG_RE.match(v):
            raise ValueError("step id must match ^[a-zA-Z0-9_-]+$")
        return v


class Transition(BaseModel):
    id: Optional[str] = None
    from_step: str = Field(..., alias="from", description="Source step id")
    to: str = Field(..., description="Target step id")
    condition: Optional[str] = None  # expression, e.g. "variables.approved == true"
    label: Optional[str] = None

    model_config = {"populate_by_name": True}


class LLMConfig(BaseModel):
    provider: str = Field(default="mistral", description="LLM provider id")
    model: str = Field(default="mistral-small-latest")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    system_prompt: Optional[str] = None
    max_tokens: Optional[int] = Field(default=None, ge=1)


class TrackSettings(BaseModel):
    timeout_hours: Optional[int] = Field(default=None, ge=1)
    max_retries: int = Field(default=0, ge=0)
    allow_parallel: bool = False


class Track(BaseModel):
    id: str = Field(..., description="Track slug")
    name: str = Field(..., min_length=1)
    version: int = Field(default=1, ge=1)
    status: TrackStatus = Field(default=TrackStatus.draft)
    team: str = Field(..., min_length=1)
    description: Optional[str] = None
    variables: Dict[str, Any] = Field(default_factory=dict)
    steps: List[Step] = Field(default_factory=list)
    transitions: List[Transition] = Field(default_factory=list)
    integrations: Dict[str, Any] = Field(default_factory=dict)
    settings: TrackSettings = Field(default_factory=TrackSettings)
    llm_config: Optional[LLMConfig] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @field_validator("id", "team")
    @classmethod
    def validate_slug(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError("must match ^[a-zA-Z0-9_-]+$")
        return v

    def touch(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if not self.created_at:
            self.created_at = now
        self.updated_at = now


# ---------------------------------------------------------------------------
# Run / History
# ---------------------------------------------------------------------------

class HistoryEntry(BaseModel):
    step_id: Optional[str] = None
    type: str = Field(default="message", description="message | system | step_enter | step_exit")
    text: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    user_id: Optional[str] = None


class Run(BaseModel):
    run_id: str = Field(..., description="Unique run identifier")
    user_id: str = Field(..., min_length=1)
    team: str = Field(..., min_length=1)
    track_id: str = Field(..., min_length=1)
    track_version: int = Field(..., ge=1)
    current_step: Optional[str] = None
    status: RunStatus = Field(default=RunStatus.active)
    variables: Dict[str, Any] = Field(default_factory=dict)
    history: List[HistoryEntry] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @field_validator("run_id", "team", "track_id")
    @classmethod
    def validate_slug(cls, v: str) -> str:
        # run_id may be uuid-like, allow broader but still safe
        if not v or len(v) > 128:
            raise ValueError("invalid identifier length")
        return v

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Request / Response helpers
# ---------------------------------------------------------------------------

class CreateTrackRequest(BaseModel):
    id: str
    name: str
    team: str
    description: Optional[str] = None
    variables: Dict[str, Any] = Field(default_factory=dict)
    steps: List[Step] = Field(default_factory=list)
    transitions: List[Transition] = Field(default_factory=list)
    integrations: Dict[str, Any] = Field(default_factory=dict)
    settings: Optional[TrackSettings] = None
    llm_config: Optional[LLMConfig] = None
    status: Optional[TrackStatus] = None

    @field_validator("id", "team")
    @classmethod
    def validate_slug(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError("must match ^[a-zA-Z0-9_-]+$")
        return v


class StartRunRequest(BaseModel):
    track_id: str
    team: str
    user_id: str
    variables: Optional[Dict[str, Any]] = None


class MessageRequest(BaseModel):
    text: str = Field(..., min_length=1)
    user_id: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None


class AdvanceRequest(BaseModel):
    target_step: Optional[str] = None
    variables: Optional[Dict[str, Any]] = None
    status: Optional[RunStatus] = None
    text: Optional[str] = None


class LLMGlobalConfig(BaseModel):
    provider: str = Field(default="mistral")
    model: str = Field(default="mistral-small-latest")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    api_key_configured: bool = False
    base_url: Optional[str] = None
    max_tokens: Optional[int] = Field(default=None, ge=1)
    extra: Dict[str, Any] = Field(default_factory=dict)
