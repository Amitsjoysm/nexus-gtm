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
