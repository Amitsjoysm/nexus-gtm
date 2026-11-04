# nexus/agents/email_style.py
"""The workspace's own voice, offered to the draft writer as structure — never as facts.

Asked for by the product owner 2026-09-16: "user can add some of his/her standard emails and our
agent adapts to it". A sample is the clearest possible brief; it is also the most dangerous input
this prompt takes, because a real SDR email names a real customer and a real number:

    We helped Globex cut onboarding 40%. Worth 15 minutes?

Copied into a draft for a different buyer, that is a fabricated case study sent to a prospect — the
exact failure `EMAIL_RULES` ("use only facts given above") exists to prevent, arriving through a
door the rules were not watching. So every sample travels with the instruction that it is a shape to
follow, not a claim to reuse, and the fact rules still apply beneath it.

Tone and length are separate from samples because a workspace with no samples still wants them, and
because a number in the prompt is cheaper than a paragraph of prose asking for brevity.
"""
from __future__ import annotations

#: Every sample is prompt tokens on every draft this workspace generates. Three is a voice; thirty
#: is a bill, and the later ones dilute rather than sharpen.
MAX_SAMPLES = 3

#: One sample longer than this is a newsletter, not a cold email, and it would dominate the prompt.
MAX_SAMPLE_CHARS = 1200


def _style(settings: dict | None) -> dict:
    raw = (settings or {}).get("style")
    return raw if isinstance(raw, dict) else {}


def samples(settings: dict | None) -> list[str]:
    """The workspace's example emails, trimmed and capped. Never raises."""
    try:
        raw = _style(settings).get("samples")
        if not isinstance(raw, list):
            return []
        out = [str(s).strip()[:MAX_SAMPLE_CHARS] for s in raw if str(s).strip()]
        return out[:MAX_SAMPLES]
    except Exception:  # noqa: BLE001 - a malformed blob must cost the style, not the draft
        return []


#: What a first email may ask for. "interest" is the playbook's one-word question and the default;
#: "meeting" is a workspace's explicit choice to ask for a short meeting instead (2026-10-01).
FIRST_ASKS = ("interest", "meeting")


def draft_options(settings: dict | None) -> dict:
    """The two style choices both the prompt and the draft check read, so they cannot disagree.

    ``word_cap`` is the workspace's target length when it set one, else None (the rules' 90).
    ``first_ask`` is "meeting" only when the workspace chose it. Never raises: a malformed blob
    leaves the defaults, which are exactly the behaviour before these options existed.
    """
    try:
        style = _style(settings)
        length = style.get("length_words")
        cap = int(length) if length not in (None, "") else None
        ask = str(style.get("first_ask") or "interest")
        return {"word_cap": cap if cap and cap > 0 else None,
                "first_ask": ask if ask in FIRST_ASKS else "interest"}
    except Exception:  # noqa: BLE001
        return {"word_cap": None, "first_ask": "interest"}


def ps_line_on(settings: dict | None) -> bool:
    """Whether first emails may carry the optional verified P.S. (`copy.PS_RULE`). Off unless the
    workspace switched it on, and only a real boolean counts: the panel posts JSON, and the string
    "false" is truthy in Python."""
    try:
        return _style(settings).get("ps_line") is True
    except Exception:  # noqa: BLE001
        return False


def style_prompt(settings: dict | None) -> str:
    """The style block for the messaging prompt, or "" when the workspace has configured nothing.

    Empty really means empty: an instruction scaffold with no content is something the model tries
    to satisfy anyway, and it would push drafts toward a voice nobody asked for.
    """
    try:
        style = _style(settings)
        tone = str(style.get("tone") or "").strip()
        length = style.get("length_words")
        examples = samples(settings)
        if not tone and not length and not examples:
            return ""

        parts: list[str] = ["HOUSE STYLE:"]
        if tone:
            parts.append(f"Tone this workspace writes in: {tone}.")
        if length:
            try:
                parts.append(f"Aim for about {int(length)} words.")
            except (TypeError, ValueError):
                pass
        if examples:
            parts.append(
                "Emails this workspace considers good. Follow their STRUCTURE, rhythm and voice "
                "ONLY. Every fact, customer name, metric and claim in them belongs to a different "
                "deal: never reuse one, and never write a number that is not in the context above."
            )
            for i, sample in enumerate(examples, 1):
                parts.append(f"--- example {i} ---\n{sample}")
        return "\n".join(parts) + "\n"
    except Exception:  # noqa: BLE001
        return ""


# ---- a mailbox's own style (2026-11-04) ----------------------------------------------------------
#
# Asked by the SDR team: "signatures and style examples in the mailbox, set independently for each
# mailbox". Decided with the owner: each mailbox sets its own, and a field it leaves unset uses the
# workspace's. So the merge is per FIELD: a mailbox that set only its samples keeps the workspace's
# tone and length. Stored on `mailbox_connections.style` (migration 0079).

#: The fields a mailbox may set for itself; the signature is its own column already.
MAILBOX_STYLE_KEYS = ("tone", "length_words", "first_ask", "ps_line", "samples")


def _is_set(value) -> bool:
    return value is not None and value != "" and value != []


def merged_settings(workspace: dict | None, mailbox_style: dict | None) -> dict:
    """The workspace's email settings with this mailbox's own style laid over them, field by field.

    What every reader of the style takes (`draft_options`, `style_prompt`, `ps_line_on`,
    `samples`), so a draft from a mailbox and the check of that draft read the same thing. A
    mailbox with no style of its own gets the workspace's exactly. Never raises."""
    settings = dict(workspace or {})
    try:
        base = dict(_style(settings))
        own = mailbox_style if isinstance(mailbox_style, dict) else {}
        for key in MAILBOX_STYLE_KEYS:
            if _is_set(own.get(key)):
                base[key] = own[key]
        settings["style"] = base
    except Exception:  # noqa: BLE001 - a malformed blob must cost the mailbox's style, not the draft
        pass
    return settings
