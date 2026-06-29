"""Request/response models for the /network router. OAuth is never exposed."""
from __future__ import annotations

from pydantic import BaseModel, Field

from nexus.network.connectors.base import RawIdentity, Touchpoint


class ConnectRequest(BaseModel):
    provider: str
    external_account_id: str
    display_email: str = ""


class PatchAccountRequest(BaseModel):
    pooling_enabled: bool | None = None
    status: str | None = None


class NetworkAccountOut(BaseModel):
    id: str
    provider: str
    external_account_id: str
    display_email: str
    status: str
    pooling_enabled: bool
    last_synced_at: str | None = None


class ImportRequest(BaseModel):
    identities: list[RawIdentity] = Field(default_factory=list)
    touchpoints: list[Touchpoint] = Field(default_factory=list)
    next_cursor: str | None = None


class IngestResultOut(BaseModel):
    identities: int
    new_persons: int
    new_edges: int


class PersonOut(BaseModel):
    id: str
    primary_email: str | None
    full_name: str
    title: str
    company: str
    location: str


class SearchRequest(BaseModel):
    query: str
    limit: int = Field(default=20, ge=1, le=100)


class SearchHitOut(BaseModel):
    person: PersonOut
    score: float
    best_strength: int
    broker_member_ids: list[str]


class IntroPathOut(BaseModel):
    broker_member_id: str
    broker_user_id: str
    relation: str
    strength: int
    last_touch_at: str | None
    provider: str
