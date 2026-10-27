"""The team's dispositions and categories, one list for calling and email reps (2026-10-26).

Supplied by the SDR team with "best UX with easy drop downs for various categories" and decided
with the owner (D11): every outcome is recorded, and a few take a SAFE action, never a destructive
one:

* DNC / Opt Out / Unsubscribe: the person goes on do-not-contact (email) and is not called again.
* Bad Number, Number Disconnected: the phone is marked bad (kept, not deleted).
* Bounce: the email address is removed and remembered as rejected (the save policy).
* Callback, Follow-Up Required: the call stays open, due on the date given.
* Outside ICP, Existing Customer, Duplicate Lead: the account is flagged; nothing is deleted.
* Wrong Person, Reference Shared: an Inbox task asks for the right contact.
* Everything else: recorded only.

Channel-only values appear only on their channel: Bounce is email's, the call attempts and
Gatekeeper are calling's. The codes are stored on ``call_activities.disposition`` (24 characters),
and the eight that existed before stay valid for the rows that hold them.
"""
from __future__ import annotations

from dataclasses import dataclass

CALL = "call"
EMAIL = "email"
BOTH = frozenset({CALL, EMAIL})

#: What logging a disposition does besides recording it.
RECORD = "record"
DO_NOT_CONTACT = "do_not_contact"
PHONE_BAD = "phone_bad"
EMAIL_BAD = "email_bad"
DATED_TASK = "dated_task"
FLAG_ACCOUNT = "flag_account"
FIND_RIGHT_CONTACT = "find_right_contact"


@dataclass(frozen=True)
class Disposition:
    code: str
    label: str
    category: str
    channels: frozenset = BOTH
    action: str = RECORD
    #: The call stays open to try again (the old REQUEUE set's meaning).
    requeue: bool = False


#: (key, label), in the order the team listed them.
CATEGORIES: tuple[tuple[str, str], ...] = (
    ("positive", "Positive Outcome"),
    ("follow_up", "Follow-Up"),
    ("call_attempt", "Call Attempt"),
    ("negative", "Contacted - Negative Outcome"),
    ("contact_issues", "Contact Issues"),
    ("qualification", "Company Qualification"),
    ("compliance", "Compliance"),
    ("gatekeeper", "Gatekeeper"),
    ("other", "Other"),
    ("bounce", "Bounce"),
)

_CALL_ONLY = frozenset({CALL})
_EMAIL_ONLY = frozenset({EMAIL})

DISPOSITIONS: tuple[Disposition, ...] = (
    Disposition("lead", "LEAD", "positive"),
    Disposition("interested_mql", "Interested - Send Information (MQL)", "positive"),
    Disposition("reference_shared", "Reference Shared", "positive", action=FIND_RIGHT_CONTACT),
    Disposition("callback", "Callback", "follow_up", action=DATED_TASK, requeue=True),
    Disposition("follow_up_required", "Follow-Up Required", "follow_up", action=DATED_TASK,
                requeue=True),
    Disposition("no_answer", "No Answer", "call_attempt", _CALL_ONLY, requeue=True),
    Disposition("voicemail", "Voicemail", "call_attempt", _CALL_ONLY),
    Disposition("busy_signal", "Busy Signal", "call_attempt", _CALL_ONLY, requeue=True),
    Disposition("opening_hang_up", "Opening Hang-Up", "call_attempt", _CALL_ONLY, requeue=True),
    Disposition("not_interested", "Not Interested", "negative"),
    Disposition("hung_up_no_interest", "Hung Up - No Interest", "negative", _CALL_ONLY),
    Disposition("not_interested_reason", "Not Interested - Reason Shared", "negative"),
    Disposition("no_budget", "No Budget / No Plans", "negative"),
    Disposition("wrong_person", "Wrong Person", "contact_issues", action=FIND_RIGHT_CONTACT),
    Disposition("bad_number", "Bad Number", "contact_issues", _CALL_ONLY, action=PHONE_BAD),
    Disposition("number_disconnected", "Number Disconnected", "contact_issues", _CALL_ONLY,
                action=PHONE_BAD),
    Disposition("existing_customer", "Existing Customer", "qualification", action=FLAG_ACCOUNT),
    Disposition("decision_elsewhere", "Decision at Another Location", "qualification"),
    Disposition("using_competitor", "Using Competitor - Not Switching", "qualification"),
    Disposition("in_house_solution", "In-House Solution", "qualification"),
    Disposition("outside_icp", "Outside ICP", "qualification", action=FLAG_ACCOUNT),
    Disposition("duplicate_lead", "Duplicate Lead", "qualification", action=FLAG_ACCOUNT),
    Disposition("engaged_with_colleague", "Already Engaged with Colleague", "qualification"),
    Disposition("supplier_registration", "Supplier Registration Required", "qualification"),
    Disposition("dnc", "DNC / Opt Out / Unsubscribe", "compliance", action=DO_NOT_CONTACT),
    Disposition("gatekeeper", "Gatekeeper", "gatekeeper", _CALL_ONLY, requeue=True),
    Disposition("other", "Other", "other"),
    Disposition("bounce", "Bounce", "bounce", _EMAIL_ONLY, action=EMAIL_BAD),
)

BY_CODE: dict[str, Disposition] = {d.code: d for d in DISPOSITIONS}

#: Codes logged before this list, kept valid so stored rows still read (and old clients still
#: log): `connected` is a call with no outcome named, `meeting_booked` is a LEAD.
LEGACY: dict[str, Disposition] = {
    "connected": Disposition("connected", "Connected", "other", _CALL_ONLY),
    "meeting_booked": Disposition("meeting_booked", "Meeting booked", "positive", _CALL_ONLY),
}

#: How the email reply classifier's categories read in the team's list, so both channels speak
#: one vocabulary. An out-of-office or another machine reply is an attempt, not an outcome.
REPLY_TO_DISPOSITION: dict[str, str] = {
    "interested": "interested_mql",
    "question": "follow_up_required",
    "referral": "reference_shared",
    "later": "callback",
    "declined": "not_interested",
    "unsubscribe": "dnc",
    "out_of_office": "other",
    "other_auto": "other",
    "unclear": "other",
}


def lookup(code: str | None) -> Disposition | None:
    return BY_CODE.get(code or "") or LEGACY.get(code or "")


def for_channel(channel: str) -> list[dict]:
    """The categories with the dispositions a rep on ``channel`` may pick, for the dropdowns."""
    out = []
    for key, label in CATEGORIES:
        values = [{"code": d.code, "label": d.label, "action": d.action,
                   "needs_date": d.action == DATED_TASK}
                  for d in DISPOSITIONS if d.category == key and channel in d.channels]
        if values:
            out.append({"key": key, "label": label, "dispositions": values})
    return out


def category_label(code: str | None) -> str:
    d = lookup(code)
    return dict(CATEGORIES).get(d.category, "") if d else ""
