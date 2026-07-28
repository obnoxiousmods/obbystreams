#!/usr/bin/env python3
"""Dry-run the UFC auto-schedule against a simulated clock.

Walks a whole event timeline — 30 hours before the first bout through the
post-fight stand-down — printing every decision and every Discord embed that
*would* be sent. Nothing is posted, no stream is touched, no network is used.

Usage::

    uv run python tools/schedule_replay.py                # canned fixtures
    uv run python tools/schedule_replay.py --live         # fetch real ESPN data
    uv run python tools/schedule_replay.py --step 15      # 15-minute clock steps
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import httpx

from obbyschedule import EspnScheduleProvider, SchedulerAction, ScheduleSettings, UfcScheduler

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures"
WEBHOOK = "https://discord.example/webhook/replay"

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
RESET = "\033[0m"


class FixtureProvider:
    """Serves recorded ESPN payloads, switching to 'final' after the last bout."""

    def __init__(self, settings: ScheduleSettings) -> None:
        self.settings = settings
        self.clock = datetime.now(UTC)
        self._cache: dict[str, dict[str, Any]] = {}

    def with_settings(self, settings: ScheduleSettings) -> FixtureProvider:
        self.settings = settings
        return self

    def _load(self, name: str) -> dict[str, Any]:
        if name not in self._cache:
            payload = json.loads((FIXTURES / f"espn_{name}.json").read_text())
            # The fixtures carry distinct ids so unit tests can tell them apart;
            # here they are three snapshots of ONE card, so pin a single id.
            for event in payload.get("events", []):
                event["id"] = "600059667"
            self._cache[name] = payload
        return self._cache[name]

    async def fetch_calendar(self):
        return EspnScheduleProvider.parse_calendar(self._load("final"), self.settings)

    async def fetch_event(self, day):
        # Replay the card's real progression: scheduled -> in progress -> final.
        event = EspnScheduleProvider.parse_events(self._load("final"), self.settings)
        if event is None:
            return None
        last = event.cards[-1].start
        first = event.cards[0].start
        if self.clock >= last + timedelta(hours=2):
            name = "final"
        elif self.clock >= first:
            name = "mid"
        else:
            name = "pre"
        return EspnScheduleProvider.parse_events(self._load(name), self.settings)


class LiveProvider(EspnScheduleProvider):
    """Real ESPN, read-only — used with --live to sanity-check today's data."""


class DryRunNotifier:
    """Collects embeds instead of posting them."""

    def __init__(self, builder) -> None:
        self.builder = builder
        self.active = True
        self.sent: list[dict[str, Any]] = []

    def with_settings(self, settings):
        return self

    async def send_embed(self, embed: dict[str, Any]) -> bool:
        self.sent.append(embed)
        return True


class DryRunCockpit:
    """Stands in for the cockpit's start/stop primitives."""

    def __init__(self) -> None:
        self.running = False
        self.actions: list[str] = []

    async def start(self, reason: str) -> bool:
        self.running = True
        self.actions.append(f"START ({reason})")
        return True

    async def stop(self, reason: str) -> bool:
        self.running = False
        self.actions.append(f"STOP ({reason})")
        return True

    async def refresh(self, reason: str) -> None:
        self.actions.append(f"refresh sources ({reason})")


def describe_embed(embed: dict[str, Any]) -> str:
    title = embed.get("title", "?")
    return f"{CYAN}DISCORD{RESET} {title}"


async def replay(step_minutes: int, live: bool) -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="schedule-replay-"))
    section = {
        "state_path": str(tmp / "state.json"),
        "notify": {"discord_webhook_url": WEBHOOK},
    }
    settings = ScheduleSettings.from_config(section)
    cockpit = DryRunCockpit()

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        scheduler = UfcScheduler(
            client=client,
            load_config=lambda: {"schedule": section},
            start_stream=cockpit.start,
            stop_stream=cockpit.stop,
            refresh_sources=cockpit.refresh,
        )
        notifier = DryRunNotifier(scheduler.notifier.builder)
        scheduler.bind(notifier=notifier)

        provider: Any
        if live:
            provider = LiveProvider(client, settings)
            scheduler.bind(provider=provider)
            calendar = await provider.fetch_calendar()
            if not calendar:
                print(f"{RED}ESPN returned no calendar entries{RESET}")
                return 1
            # Same selection production uses, against the real clock, so this
            # replays the card the cockpit would actually be waiting on.
            target = UfcScheduler.select_target(calendar, datetime.now(UTC))
            if target is None:
                print(f"{RED}no upcoming UFC card on the ESPN calendar{RESET}")
                return 1
            print(f"{BOLD}Live ESPN{RESET}: next card is {target.label} at {target.start.isoformat()}")
            anchor = target.start
            current = await provider.fetch_event(target.start.date())
            if current is not None and current.is_final:
                print(
                    f"{YELLOW}note{RESET}: ESPN already reports this card final, so the rewound clock replays it "
                    f"out of order. In production sweep_stale() suppresses those past milestones — use --live only "
                    f"for a card that has not happened yet."
                )
        else:
            provider = FixtureProvider(settings)
            scheduler.bind(provider=provider)
            anchor = datetime(2026, 7, 25, 13, 0, tzinfo=UTC)
            print(f"{BOLD}Fixture replay{RESET}: Ankalaev vs. Guskov, first bout {anchor.isoformat()}")

        print(f"{DIM}clock steps of {step_minutes}m; nothing is posted or started for real{RESET}\n")

        start = anchor - timedelta(hours=30)
        end = anchor + timedelta(hours=10)
        clock = start
        seen_embeds = 0
        seen_actions = 0

        while clock <= end:
            if isinstance(provider, FixtureProvider):
                provider.clock = clock
            await scheduler.tick(stream_running=cockpit.running, now=clock)

            decision = scheduler.last_decision
            offset = (clock - anchor).total_seconds() / 3600.0
            stamp = f"{clock.strftime('%a %H:%M')}Z  T{offset:+06.2f}h"

            for embed in notifier.sent[seen_embeds:]:
                print(f"  {stamp}  {describe_embed(embed)}")
            seen_embeds = len(notifier.sent)

            for action in cockpit.actions[seen_actions:]:
                colour = GREEN if action.startswith("START") else RED if action.startswith("STOP") else YELLOW
                print(f"  {stamp}  {colour}{action}{RESET}")
            seen_actions = len(cockpit.actions)

            if decision.action is not SchedulerAction.IDLE:
                print(f"  {stamp}  {DIM}decision={decision.action.value} ({decision.reason}){RESET}")

            clock += timedelta(minutes=step_minutes)

        print(f"\n{BOLD}Summary{RESET}")
        print(f"  embeds that would be sent : {len(notifier.sent)}")
        for embed in notifier.sent:
            print(f"    - {embed.get('title')}")
        print(f"  cockpit actions           : {len(cockpit.actions)}")
        for action in cockpit.actions:
            print(f"    - {action}")
        print(f"  stream running at the end : {cockpit.running}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--step", type=int, default=10, help="simulated clock step in minutes (default: 10)")
    parser.add_argument("--live", action="store_true", help="fetch the real ESPN calendar instead of fixtures")
    args = parser.parse_args()
    return asyncio.run(replay(args.step, args.live))


if __name__ == "__main__":
    raise SystemExit(main())
