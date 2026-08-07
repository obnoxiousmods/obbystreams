"""ESPN payload parsing for the UFC auto-schedule.

Fixtures are trimmed captures of the real scoreboard for the 2026-07-25 Abu Dhabi
card (Ankalaev vs. Guskov), plus derived pre-event / mid-event / cancelled-bout
variants. Every test here is offline.
"""

import json
import pathlib

import pytest

from obbyschedule import EspnScheduleProvider, ScheduleSettings, parse_iso
from obbyschedule.espn import card_labels_for

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / f"espn_{name}.json").read_text())


def parse(name, settings):
    """Parse a fixture, asserting a match was found so tests read cleanly."""
    event = EspnScheduleProvider.parse_events(load(name), settings)
    assert event is not None
    return event


def iso(raw):
    parsed = parse_iso(raw)
    assert parsed is not None
    return parsed


@pytest.fixture
def settings():
    return ScheduleSettings.from_config({})


# ---------------------------------------------------------------- timestamps
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-07-25T13:00Z", "2026-07-25T13:00:00+00:00"),
        ("2026-07-25T13:00:00Z", "2026-07-25T13:00:00+00:00"),
        ("2026-08-16T00:00Z", "2026-08-16T00:00:00+00:00"),
    ],
)
def test_parse_iso_handles_espn_shapes(raw, expected):
    """ESPN emits minute-precision timestamps with no seconds."""
    assert iso(raw).isoformat() == expected


@pytest.mark.parametrize("raw", [None, "", "not-a-date", 42, {}])
def test_parse_iso_rejects_junk(raw):
    assert parse_iso(raw) is None


def test_parse_iso_assumes_utc_when_naive():
    assert iso("2026-07-25T13:00:00").tzinfo is not None


# ------------------------------------------------------------------ calendar
def test_calendar_filters_out_contender_series(settings):
    payload = load("final")
    raw_labels = [row["label"] for row in payload["leagues"][0]["calendar"]]
    kept = EspnScheduleProvider.parse_calendar(payload, settings)

    assert any("Contender Series" in label for label in raw_labels), "fixture should contain DWCS rows"
    assert len(kept) < len(raw_labels)
    assert all("Contender Series" not in entry.label for entry in kept)
    assert all(entry.label.startswith("UFC") for entry in kept)


def test_calendar_is_sorted_by_start(settings):
    entries = EspnScheduleProvider.parse_calendar(load("final"), settings)
    assert [entry.start for entry in entries] == sorted(entry.start for entry in entries)


def test_calendar_include_pattern_can_be_narrowed():
    """A numbered-PPV-only operator should get no Fight Nights."""
    narrow = ScheduleSettings.from_config({"include_pattern": r"^UFC\s+\d+"})
    entries = EspnScheduleProvider.parse_calendar(load("final"), narrow)
    assert entries
    assert all("Fight Night" not in entry.label for entry in entries)


def test_calendar_empty_payload_is_safe(settings):
    assert EspnScheduleProvider.parse_calendar({}, settings) == ()
    assert EspnScheduleProvider.parse_calendar({"leagues": []}, settings) == ()


# --------------------------------------------------------------------- event
def test_parse_event_segments_cards_by_start_time(settings):
    event = parse("final", settings)

    assert event.event_id == "600059667"
    assert event.name == "UFC Fight Night: Ankalaev vs. Guskov"
    assert [card.label for card in event.cards] == ["Prelims", "Main card"]
    assert [card.bout_count for card in event.cards] == [7, 5]
    assert event.first_card_start is not None
    assert event.first_card_start.isoformat() == "2026-07-25T13:00:00+00:00"
    assert event.cards[-1].start.isoformat() == "2026-07-25T16:00:00+00:00"


def test_parse_event_extracts_venue_and_main_event(settings):
    event = parse("final", settings)

    assert event.venue == "Etihad Arena"
    assert event.city == "Abu Dhabi, United Arab Emirates"
    assert event.main_event_bout == "Magomed Ankalaev vs. Bogdan Guskov"
    assert event.main_event_winner == "Magomed Ankalaev"


def test_finished_card_is_final(settings):
    event = parse("final", settings)
    assert event.is_final is True
    assert all(card.all_final for card in event.cards)


def test_pre_event_card_is_not_final(settings):
    event = parse("pre", settings)

    assert event.is_final is False
    assert event.main_event_winner is None
    assert [card.completed_bouts for card in event.cards] == [0, 0]


def test_mid_event_reports_partial_progress(settings):
    """Prelims done, main card underway — must not read as final."""
    event = parse("mid", settings)

    assert event.is_final is False
    assert event.cards[0].all_final is True
    assert event.cards[1].all_final is False
    assert event.cards[1].completed_bouts == 1


def test_cancelled_bout_does_not_hold_the_card_open(settings):
    """A scratched prelim never completes; the card must still finish."""
    event = parse("cancelled", settings)
    assert event.is_final is True


def test_parse_events_ignores_non_matching_names(settings):
    payload = load("final")
    payload["events"][0]["name"] = "Dana White's Contender Series: Season 10, Week 1"
    assert EspnScheduleProvider.parse_events(payload, settings) is None


def test_parse_event_without_competitions_is_none():
    assert EspnScheduleProvider.parse_event({"id": "1", "name": "UFC 999"}) is None


# ------------------------------------------------------------- card labelling
@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, ()),
        (1, ("Main card",)),
        (2, ("Prelims", "Main card")),
        (3, ("Early prelims", "Prelims", "Main card")),
        (4, ("Card 1", "Card 2", "Card 3", "Main card")),
    ],
)
def test_card_labels_always_end_with_main_card(count, expected):
    assert card_labels_for(count) == expected


@pytest.mark.asyncio
async def test_espn_is_not_fetched_with_the_scraper_user_agent():
    """The cockpit's shared httpx client sends a Firefox UA because the
    stream-source scrapers need one. On 2026-08-04 ESPN began answering 403 to
    browser-like agents on this endpoint while still serving plain library ones,
    which silently killed every UFC alert - no scoreboard means no tracked card,
    no milestones, no Discord post, and nothing else depends on ESPN so the only
    symptom was silence."""
    import httpx

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("User-Agent", "")
        return httpx.Response(200, json={"leagues": [], "events": []})

    scraper_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.0"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers={"User-Agent": scraper_ua}
    ) as client:
        provider = EspnScheduleProvider(client, ScheduleSettings())
        await provider.fetch_scoreboard()

    assert "Mozilla" not in seen["ua"], f"sent a browser UA to ESPN: {seen['ua']}"
    assert "httpx" in seen["ua"]
