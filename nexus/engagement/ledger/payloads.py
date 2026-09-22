"""What each engagement event's ``refs`` and ``payload`` carry — the contract between emitters and
the dataset builders (spec §18.1, §18.4, §18.5).

Emitters in phases 07-10 build these dicts; ``datasets.build`` reads them. A field an emitter omits
reads as absent, never as an error: an example missing its context is skipped with a quality flag
rather than breaking the build for every other event. Keeping the shapes in one module is what lets a
builder written in phase 06 read an event first emitted in phase 09.
"""
from __future__ import annotations

from typing import TypedDict


class EngagementRefs(TypedDict, total=False):
    account_id: str
    contact_id: str
    campaign_id: str
    enrollment_id: str
    thread_id: str
    message_id: str
    mailbox_id: str
    #: On reply events: the outbound message the reply answers (from In-Reply-To / thread matching).
    answered_message_id: str
    classification_id: str


class DraftCreated(TypedDict, total=False):        # draft.created
    step_index: int
    kind: str                                       # step | reengage | response
    context_pack: str                               # the full prompt context the model saw
    subject: str
    body: str
    quality_problems: list[str]
    prompt_version: str


class DraftEdited(TypedDict, total=False):         # draft.edited
    context_pack: str
    ai_subject: str
    ai_body: str
    subject: str
    body: str
    edit_distance: int


class MessageSent(TypedDict, total=False):         # message.sent
    kind: str
    step_index: int
    subject: str
    body: str
    context_pack: str
    sent_local_hour: int
    sent_local_weekday: int
    contact_timezone: str
    mailbox_provider: str
    days_since_last_touch: int
    persona: dict                                   # {"title", "seniority"}
    account: dict                                   # {"industry", "employee_count", "country"}
    signal_types: list[str]
    personalisation_facts: list[str]


class ReplyReceived(TypedDict, total=False):       # reply.received
    inbound_kind: str                               # human | auto_reply | bounce | other_auto
    local_hour: int
    local_weekday: int
    response_latency_s: int
    body: str
    thread: list[dict]                              # [{"direction", "at", "body"}], oldest first


class ReplyClassified(TypedDict, total=False):     # reply.classified
    category: str
    confidence: float
    date_phrase: str
    resolved_date: str                              # ISO date
    label_source: str                               # ai | deterministic
    body: str
    thread: list[dict]
    prompt_version: str


class ReplyCorrected(TypedDict, total=False):      # reply.corrected
    ai_category: str
    sdr_category: str
    resolved_date: str


class ResponseSent(TypedDict, total=False):        # response.sent
    subject: str
    body: str
    ai_body: str
    conversation: list[dict]


POSITIVE_CATEGORIES = frozenset({"interested", "question", "referral"})
