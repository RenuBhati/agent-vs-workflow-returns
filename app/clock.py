"""Where "today" comes from.

Rules never call date.today() themselves; they are handed a Clock. Tests,
scenario runs and the demo use FixedClock with the fixture's date, so date
rules replay the same way whenever they run.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")


class Clock(Protocol):
    def today(self) -> date: ...


@dataclass(frozen=True)
class FixedClock:
    day: date

    def today(self) -> date:
        return self.day


class LondonClock:
    """The real calendar date in London, which is what a UK shopper means by 'today'."""

    def __init__(self, now=lambda: datetime.now(LONDON)):
        self._now = now  # injectable so the timezone handling can be tested

    def today(self) -> date:
        return self._now().astimezone(LONDON).date()
