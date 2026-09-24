"""When to send, for "at a set time" steps and for moving one person's next step (spec §8, §18.5).

Pure. The best time comes from what the display rules allow: a person's own pattern, else their
company's, else the engine's default working-morning time. A campaign's suggestion is the hour most
of its people with a pattern reply at, needing at least two of them to agree, so one prolific
replier does not set everyone's send time.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass

from nexus.engagement.insights.rules import WEEKDAYS, Insight

DEFAULT_HOUR = 9
DEFAULT_MINUTE = 30
MIN_AGREEING = 2


@dataclass(slots=True)
class BestTime:
    hour: int
    minute: int
    weekday: int | None
    #: "person" | "company" | "campaign" | "default"
    source: str
    text: str

    @property
    def clock(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"

    def as_dict(self) -> dict:
        return {**asdict(self), "clock": self.clock}


def _default() -> BestTime:
    return BestTime(DEFAULT_HOUR, DEFAULT_MINUTE, None, "default",
                    "No pattern yet: their working morning is the safe choice.")


def for_person(person: Insight, company: Insight | None = None) -> BestTime:
    for insight, source, whose in ((person, "person", "They"), (company, "company", "People there")):
        if insight is not None and insight.level == "pattern" and insight.best_hour is not None:
            day = f" on {WEEKDAYS[insight.best_weekday]}" if insight.best_weekday is not None else ""
            return BestTime(insight.best_hour, 0, insight.best_weekday, source,
                            f"{whose} usually reply{day} around {insight.best_hour:02d}:00 "
                            "their time.")
    return _default()


def for_campaign(insights: list[Insight]) -> BestTime:
    hours = [i.best_hour for i in insights if i.level == "pattern" and i.best_hour is not None]
    if hours:
        hour, agreeing = Counter(hours).most_common(1)[0]
        if agreeing >= MIN_AGREEING:
            return BestTime(hour, 0, None, "campaign",
                            f"{agreeing} of the people here usually reply around {hour:02d}:00 "
                            "their time.")
    return _default()
