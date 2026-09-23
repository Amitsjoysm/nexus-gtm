"""The signal day window — one policy, read by every screen that lists signals and by the AI.

Decided with the product owner 2026-09-23, and set by a superadmin from the Control plane:

* ``signal_window_user_choice`` ON (the default): each user picks a window in the top bar, starting
  from ``signal_window_default``, and the server honours what the client asks for.
* OFF: the picker is gone and the server ENFORCES the default on every list, whatever a client
  sends. A policy the server does not enforce is a preference an old tab or a bookmark ignores.

``signal_window_default`` defaults to ``all``, so a deployment that never opens the panel behaves
exactly as before. AI drafts and research always use the default — a draft has no top bar, and the
superadmin's default is the platform's statement of what counts as recent.

The window is about signals: an Inbox task or alert that no signal raised always shows.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

logger = logging.getLogger("nexus.ingestion.window")

#: (stored value, label, days). One definition, served to the top bar by ``GET /signals/window``
#: and offered by the Control plane, so the picker and the setting cannot disagree.
WINDOW_OPTIONS: tuple[tuple[str, str, int | None], ...] = (
    ("7", "Weekly", 7),
    ("14", "Fortnightly", 14),
    ("30", "Monthly", 30),
    ("90", "Quarterly", 90),
    ("180", "Half-yearly", 180),
    ("365", "Yearly", 365),
    ("all", "All time", None),
)
OPTION_VALUES: tuple[str, ...] = tuple(value for value, _, _ in WINDOW_OPTIONS)
_DAYS = {value: days for value, _, days in WINDOW_OPTIONS}


def user_choice() -> bool:
    from nexus.core.config import get_settings

    return bool(get_settings().signal_window_user_choice)


def default_days() -> int | None:
    """The superadmin's window in days, or ``None`` for all time.

    A value the reader cannot parse — one set through the environment, where no validator runs —
    hides NOTHING. Guessing a window would silently drop signals from every screen.
    """
    from nexus.core.config import get_settings

    raw = str(get_settings().signal_window_default or "all").strip().lower()
    if raw not in _DAYS:
        logger.warning("signal_window_default %r is not a known window; treating it as all time", raw)
        return None
    return _DAYS[raw]


def effective_days(requested: int | None) -> int | None:
    """The window a list applies: the client's own choice, unless the superadmin has taken it."""
    if not user_choice():
        return default_days()
    return requested


def ai_window_days() -> int | None:
    """The window AI drafts and research cite from. Always the platform default: no top bar."""
    return default_days()


def since(days: int | None) -> datetime | None:
    if not days:
        return None
    from nexus.core.db import utcnow

    return utcnow() - timedelta(days=days)


def raised_by_a_recent_signal(model, ts, cutoff: datetime | None):
    """A WHERE clause for an Inbox task or alert: no signal behind it, or one inside the window.

    ``None`` when there is no window, so callers add nothing. A subquery rather than a join keeps
    the caller's ORDER BY and LIMIT untouched, so the limit applies to rows that will be shown.
    """
    if cutoff is None:
        return None
    from sqlalchemy import or_, select

    from nexus.models.signal import SignalEvent

    recent = select(SignalEvent.id).where(
        SignalEvent.tenant_id == ts.tenant_id, SignalEvent.occurred_at >= cutoff
    )
    return or_(model.signal_id.is_(None), model.signal_id.in_(recent))
