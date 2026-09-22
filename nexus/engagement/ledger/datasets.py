"""One archived event → pseudonymised training rows and identified insights facts (spec §18.4, §18.5).

**Pure.** ``build(document, secret)`` takes what the archive holds and returns plain dataclasses;
`builder.py` is the only part that touches a store. That split is what makes the datasets testable
without three Postgres servers, and what lets a scrubber fix be replayed over the archive.

The shapes are standard so any model family can train on them without reshaping:

| dataset | anchor event | record |
|---|---|---|
| ``sft_outreach_email`` | ``message.sent`` | chat messages: context pack → the email as sent |
| ``pref_outreach_email`` | ``draft.edited`` | ``prompt`` / ``chosen`` (the SDR's) / ``rejected`` (the AI's) |
| ``cls_reply`` | ``reply.classified`` | reply + thread → category, resolved date, label source |
| ``sft_reply_response`` | ``response.sent`` | conversation → the SDR's final response |
| ``tab_engagement`` | ``message.sent`` | features → replied, positive, response latency |
| ``ai_calls`` | ``ai.call`` | prompt version, model, inputs, outputs, tokens, latency |

**Labels arrive later than the example they belong to**, which is the whole difficulty: a reply is a
different event, hours or days after the send. So a builder emits two kinds of thing — an ``Example``
(upsert, keyed by a hash of the dataset and its anchor) and a ``Patch`` (a merge into one object of
an existing example, keyed the same way). A patch for an example that does not exist is dropped: the
archive is ordered by arrival, and a reply cannot be archived before the message it answers.

An example is never overwritten by a rebuild: the upsert merges the stored labels OVER the fresh
ones, so replaying the archive cannot un-learn a label a later event set.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from nexus.engagement.ledger.payloads import POSITIVE_CATEGORIES
from nexus.engagement.ledger.pseudonym import company_key, person_key, split_for, workspace_key
from nexus.engagement.ledger.scrub import SCRUBBER_VERSION, Scrubber

SCHEMA_VERSION = 1

SYSTEM_OUTREACH = ("You are an SDR writing a first-touch email. Use only the facts in the context; "
                   "never invent a customer, a metric or a case study.")
SYSTEM_RESPONSE = "You are an SDR replying to a prospect's answer, in the SDR's own voice."

#: Which refs name a person, a company or a workspace actor, and so become HMAC keys in training.
_REF_KINDS = {"contact_id": "contact", "account_id": "account", "mailbox_id": "mailbox",
              "user_id": "user", "person_id": "person", "company_id": "company"}


@dataclass(slots=True)
class Example:
    dataset: str
    example_id: str
    anchor: str
    workspace_key: str
    person_keys: list[str]
    created_at: str
    record: dict
    consent_terms_version: str = ""
    quality: dict = field(default_factory=dict)
    source_event_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Patch:
    """A merge into ``record -> <path>`` of the example anchored on ``anchor``."""
    dataset: str
    example_id: str
    path: str
    values: dict
    source_event_id: str


@dataclass(slots=True)
class Fact:
    event_id: str
    fact_type: str
    person_email: str
    person_key: str
    company_domain: str
    workspace_key: str
    occurred_at: str
    local_hour: int | None = None
    local_weekday: int | None = None
    response_latency_s: int | None = None
    category: str = ""
    attrs: dict = field(default_factory=dict)


@dataclass(slots=True)
class FactCategory:
    """A classification arriving after the reply fact it describes."""
    message_id: str
    category: str


@dataclass(slots=True)
class Built:
    event: dict | None = None
    examples: list[Example] = field(default_factory=list)
    patches: list[Patch] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    fact_categories: list[FactCategory] = field(default_factory=list)


def example_id(dataset: str, anchor: str) -> str:
    """Stable, and not a raw application id: the training store holds no identifiers of ours."""
    return hashlib.sha256(f"{dataset}:{anchor}".encode()).hexdigest()[:32]


def pseudonymise(envelope: dict, secret: str, scrubber: Scrubber) -> dict:
    """The envelope with identities replaced by keys and free text scrubbed."""
    refs = {}
    for name, value in (envelope.get("refs") or {}).items():
        kind = _REF_KINDS.get(name)
        refs[name] = pseudonym_key(secret, kind, str(value)) if kind and value else value
    actor = dict(envelope.get("actor") or {})
    if actor.get("user_id"):
        actor["user_id"] = pseudonym_key(secret, "user", actor["user_id"])
    return {
        "event_id": envelope.get("event_id", ""),
        "event_type": envelope.get("event_type", ""),
        "schema_version": envelope.get("schema_version", SCHEMA_VERSION),
        "occurred_at": envelope.get("occurred_at", ""),
        "actor": actor,
        "refs": refs,
        "chain": envelope.get("chain") or {},
        "context": envelope.get("context") or {},
        "payload": scrubber.value(envelope.get("payload") or {}),
    }


def pseudonym_key(secret: str, kind: str, value: str) -> str:
    from nexus.engagement.ledger.pseudonym import key

    return key(secret, kind, value)


def _thread(scrubber: Scrubber, entries) -> list[dict]:
    out = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        out.append({"direction": entry.get("direction", ""), "at": entry.get("at", ""),
                    "body": scrubber.text(entry.get("body", ""))})
    return out


def build(document: dict, secret: str) -> Built:
    """One archived document (``{"envelope", "resolved"}``) → everything it contributes."""
    envelope = document.get("envelope") or {}
    resolved = document.get("resolved") or {}
    tenant_id = str(envelope.get("tenant_id") or "")
    if not tenant_id:
        return Built()

    scrubber = _scrubber(resolved)
    wkey = workspace_key(secret, tenant_id)
    emails = [e for e in (resolved.get("person_emails") or []) if e]
    pkeys = sorted({person_key(secret, email) for email in emails})
    built = Built(event={
        "event_id": envelope.get("event_id", ""),
        "event_type": envelope.get("event_type", ""),
        "schema_version": int(envelope.get("schema_version") or SCHEMA_VERSION),
        "occurred_at": envelope.get("occurred_at", ""),
        "workspace_key": wkey,
        "person_keys": pkeys,
        "body": pseudonymise(envelope, secret, scrubber),
        "scrubber_version": SCRUBBER_VERSION,
        "consent_terms_version": resolved.get("consent_terms_version") or "",
    })

    event_type = envelope.get("event_type", "")
    payload = envelope.get("payload") or {}
    refs = envelope.get("refs") or {}
    event_id = envelope.get("event_id", "")
    occurred_at = envelope.get("occurred_at", "")
    common = {"workspace_key": wkey, "person_keys": pkeys, "created_at": occurred_at,
              "source_event_ids": [event_id],
              "consent_terms_version": resolved.get("consent_terms_version") or ""}

    if event_type == "message.sent":
        _message_sent(built, scrubber, payload, refs, resolved, common, event_id, occurred_at,
                      secret, wkey, emails, pkeys)
    elif event_type == "draft.edited":
        anchor = refs.get("message_id") or event_id
        chosen = _subject_body(scrubber, payload.get("subject"), payload.get("body"))
        rejected = _subject_body(scrubber, payload.get("ai_subject"), payload.get("ai_body"))
        if chosen and rejected and chosen != rejected:
            built.examples.append(Example(
                dataset="pref_outreach_email", example_id=example_id("pref_outreach_email", anchor),
                anchor=anchor, record={"prompt": scrubber.text(payload.get("context_pack", "")),
                                       "chosen": chosen, "rejected": rejected},
                quality={"missing_context": not payload.get("context_pack"),
                         "edit_distance": payload.get("edit_distance")},
                **common))
    elif event_type == "reply.received":
        _reply_received(built, scrubber, payload, refs, resolved, event_id, occurred_at, wkey,
                        emails, secret)
    elif event_type == "reply.classified":
        _reply_classified(built, scrubber, payload, refs, common, event_id)
    elif event_type == "reply.corrected":
        _reply_corrected(built, payload, refs, event_id)
    elif event_type == "message.bounced":
        _simple_fact(built, "bounce", refs, resolved, event_id, occurred_at, wkey, emails, secret)
    elif event_type == "response.sent":
        anchor = refs.get("message_id") or event_id
        built.examples.append(Example(
            dataset="sft_reply_response", example_id=example_id("sft_reply_response", anchor),
            anchor=anchor,
            record={"messages": [{"role": "system", "content": SYSTEM_RESPONSE},
                                 *_conversation(scrubber, payload.get("conversation")),
                                 {"role": "assistant",
                                  "content": scrubber.text(payload.get("body", ""))}],
                    "labels": {"meeting": False}},
            quality={"ai_draft_kept": payload.get("body") == payload.get("ai_body")},
            **common))
    elif event_type == "outcome.recorded":
        _outcome(built, payload, refs, event_id)
    elif event_type == "ai.call":
        built.examples.append(Example(
            dataset="ai_calls", example_id=example_id("ai_calls", event_id), anchor=event_id,
            record={"agent": payload.get("agent", ""),
                    "model": (envelope.get("context") or {}).get("model", ""),
                    "prompt_version": (envelope.get("context") or {}).get("prompt_version", ""),
                    "status": payload.get("status", ""),
                    "inputs": scrubber.value(payload.get("inputs") or {}),
                    "output": scrubber.value(payload.get("output") or ""),
                    "tokens": payload.get("tokens"), "latency_ms": payload.get("latency_ms")},
            quality={"failed": payload.get("status") not in ("ok", "success", None)},
            **common))
    return built


def _scrubber(resolved: dict) -> Scrubber:
    from nexus.engagement.ledger.scrub import Known

    return Scrubber(Known(
        people=[n for n in (resolved.get("contact_name"), resolved.get("sdr_name"),
                            *(resolved.get("other_names") or [])) if n],
        companies=[c for c in (resolved.get("account_name"), resolved.get("account_domain")) if c],
        emails=[e for e in (resolved.get("contact_email"), resolved.get("sdr_email")) if e],
    ))


def _subject_body(scrubber: Scrubber, subject, body) -> str:
    subject, body = (subject or "").strip(), (body or "").strip()
    if not body:
        return ""
    head = f"Subject: {scrubber.text(subject)}\n\n" if subject else ""
    return head + scrubber.text(body)


def _conversation(scrubber: Scrubber, entries) -> list[dict]:
    out = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        role = "user" if entry.get("direction") == "in" else "assistant"
        out.append({"role": role, "content": scrubber.text(entry.get("body", ""))})
    return out


def _message_sent(built, scrubber, payload, refs, resolved, common, event_id, occurred_at, secret,
                  wkey, emails, pkeys) -> None:
    anchor = refs.get("message_id") or event_id
    body = _subject_body(scrubber, payload.get("subject"), payload.get("body"))
    if body:
        built.examples.append(Example(
            dataset="sft_outreach_email", example_id=example_id("sft_outreach_email", anchor),
            anchor=anchor,
            record={"messages": [{"role": "system", "content": SYSTEM_OUTREACH},
                                 {"role": "user",
                                  "content": scrubber.text(payload.get("context_pack", ""))},
                                 {"role": "assistant", "content": body}],
                    "labels": {"replied": False, "positive": False, "meeting": False},
                    "meta": {"kind": payload.get("kind", ""),
                             "step_index": payload.get("step_index")}},
            quality={"missing_context": not payload.get("context_pack")}, **common))
    built.examples.append(Example(
        dataset="tab_engagement", example_id=example_id("tab_engagement", anchor), anchor=anchor,
        record={"features": {
            "local_hour": payload.get("sent_local_hour"),
            "local_weekday": payload.get("sent_local_weekday"),
            "step_index": payload.get("step_index"),
            "kind": payload.get("kind", ""),
            "mailbox_provider": payload.get("mailbox_provider", ""),
            "days_since_last_touch": payload.get("days_since_last_touch"),
            "title": (payload.get("persona") or {}).get("title", ""),
            "seniority": (payload.get("persona") or {}).get("seniority", ""),
            "industry": (payload.get("account") or {}).get("industry", ""),
            "employee_count": (payload.get("account") or {}).get("employee_count"),
            "country": (payload.get("account") or {}).get("country", ""),
            "signal_types": payload.get("signal_types") or [],
            # The facts themselves are free text about a named person; how MANY were used is the
            # feature, and it carries no identity.
            "personalisation_facts": len(payload.get("personalisation_facts") or []),
        }, "labels": {"replied": False, "positive": False, "response_latency_s": None}},
        **common))
    if emails:
        built.facts.append(Fact(
            event_id=event_id, fact_type="send", person_email=emails[0],
            person_key=person_key(secret, emails[0]),
            company_domain=resolved.get("account_domain") or "", workspace_key=wkey,
            occurred_at=occurred_at, local_hour=payload.get("sent_local_hour"),
            local_weekday=payload.get("sent_local_weekday"),
            attrs={"message_id": anchor, "step_index": payload.get("step_index")}))


def _reply_received(built, scrubber, payload, refs, resolved, event_id, occurred_at, wkey, emails,
                    secret) -> None:
    answered = refs.get("answered_message_id")
    kind = payload.get("inbound_kind", "human")
    latency = payload.get("response_latency_s")
    if kind == "human" and answered:
        for dataset in ("sft_outreach_email", "tab_engagement"):
            built.patches.append(Patch(
                dataset=dataset, example_id=example_id(dataset, answered), path="labels",
                values={"replied": True, "response_latency_s": latency},
                source_event_id=event_id))
    if not emails:
        return
    fact_type = {"human": "reply", "bounce": "bounce", "auto_reply": "out_of_office"}.get(kind, "")
    if not fact_type:
        return
    attrs = {"message_id": refs.get("message_id") or event_id}
    if fact_type == "out_of_office" and payload.get("ooo_until"):
        attrs["until"] = payload["ooo_until"]
    built.facts.append(Fact(
        event_id=event_id, fact_type=fact_type, person_email=emails[0],
        person_key=person_key(secret, emails[0]),
        company_domain=resolved.get("account_domain") or "", workspace_key=wkey,
        occurred_at=occurred_at, local_hour=payload.get("local_hour"),
        local_weekday=payload.get("local_weekday"),
        response_latency_s=latency if fact_type == "reply" else None, attrs=attrs))


def _reply_classified(built, scrubber, payload, refs, common, event_id) -> None:
    anchor = refs.get("message_id") or event_id
    built.examples.append(Example(
        dataset="cls_reply", example_id=example_id("cls_reply", anchor), anchor=anchor,
        record={"input": {"reply": scrubber.text(payload.get("body", "")),
                          "thread": _thread(scrubber, payload.get("thread"))},
                "output": {"category": payload.get("category", ""),
                           "resolved_date": payload.get("resolved_date", ""),
                           "label_source": payload.get("label_source", "ai")}},
        quality={"confidence": payload.get("confidence")}, **common))
    answered = refs.get("answered_message_id")
    if answered:
        positive = payload.get("category") in POSITIVE_CATEGORIES
        for dataset in ("sft_outreach_email", "tab_engagement"):
            built.patches.append(Patch(
                dataset=dataset, example_id=example_id(dataset, answered), path="labels",
                values={"positive": positive}, source_event_id=event_id))
    built.fact_categories.append(FactCategory(message_id=anchor,
                                              category=payload.get("category", "")))


def _reply_corrected(built, payload, refs, event_id) -> None:
    anchor = refs.get("message_id") or event_id
    sdr = payload.get("sdr_category", "")
    source = "sdr_confirmed" if sdr and sdr == payload.get("ai_category") else "sdr_corrected"
    built.patches.append(Patch(
        dataset="cls_reply", example_id=example_id("cls_reply", anchor), path="output",
        values={"category": sdr, "resolved_date": payload.get("resolved_date", ""),
                "label_source": source},
        source_event_id=event_id))
    answered = refs.get("answered_message_id")
    if answered:
        for dataset in ("sft_outreach_email", "tab_engagement"):
            built.patches.append(Patch(
                dataset=dataset, example_id=example_id(dataset, answered), path="labels",
                values={"positive": sdr in POSITIVE_CATEGORIES}, source_event_id=event_id))
    built.fact_categories.append(FactCategory(message_id=anchor, category=sdr))


def _outcome(built, payload, refs, event_id) -> None:
    """A meeting is the label every outreach dataset is really after."""
    if payload.get("stage") not in ("meeting", "meeting_booked", "qualified", "won"):
        return
    anchor = (payload.get("meta") or {}).get("message_id") or refs.get("message_id")
    if not anchor:
        return
    for dataset in ("sft_outreach_email", "tab_engagement", "sft_reply_response"):
        built.patches.append(Patch(
            dataset=dataset, example_id=example_id(dataset, anchor), path="labels",
            values={"meeting": True}, source_event_id=event_id))


def _simple_fact(built, fact_type, refs, resolved, event_id, occurred_at, wkey, emails,
                 secret) -> None:
    if not emails:
        return
    built.facts.append(Fact(
        event_id=event_id, fact_type=fact_type, person_email=emails[0],
        person_key=person_key(secret, emails[0]),
        company_domain=resolved.get("account_domain") or "", workspace_key=wkey,
        occurred_at=occurred_at, attrs={"message_id": refs.get("message_id") or event_id}))


def split_of(workspace: str) -> str:
    return split_for(workspace)


def company_of(secret: str, domain: str) -> str:
    return company_key(secret, domain)
