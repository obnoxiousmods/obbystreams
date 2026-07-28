"""UFC auto-schedule for the obbystreams cockpit.

Brings the managed encode up ~15 minutes before a card's first segment, stands
it back down once every bout is decided, and posts countdown / go-live / wrap-up
embeds to Discord along the way.

The package is self-contained and never imports ``app``: the cockpit injects its
config loader and stream start/stop primitives into :class:`UfcScheduler`.
"""

from .espn import EspnScheduleProvider, parse_iso
from .models import (
    DEFAULT_TIMEZONES,
    CalendarEntry,
    CardSegment,
    Decision,
    EventPhase,
    Milestone,
    MilestoneKind,
    NotifySettings,
    SchedulerAction,
    ScheduleSettings,
    StopReason,
    UfcEvent,
)
from .notify import DiscordNotifier, EmbedBuilder
from .scheduler import UfcScheduler
from .state import ScheduleState, ScheduleStateStore

__all__ = [
    "DEFAULT_TIMEZONES",
    "CalendarEntry",
    "CardSegment",
    "Decision",
    "DiscordNotifier",
    "EmbedBuilder",
    "EspnScheduleProvider",
    "EventPhase",
    "Milestone",
    "MilestoneKind",
    "NotifySettings",
    "ScheduleSettings",
    "ScheduleState",
    "ScheduleStateStore",
    "SchedulerAction",
    "StopReason",
    "UfcEvent",
    "UfcScheduler",
    "parse_iso",
]
