"""Event-aware source discovery.

The anchor case here is the 2026-08-01 failure. The auto-schedule armed exactly
on time for UFC Fight Night: Medić vs. Rodriguez, and then ran the whole card on
the three soursignal channels that had been auto-selected a week earlier for
Ankalaev vs. Guskov — because "is this a UFC channel?" was the only question the
scraper ever asked. These tests pin the answer to "is this *tonight's* card?".
"""

from datetime import UTC, datetime, timedelta

import app
from obbyschedule import CardSegment, ScheduleSettings, UfcEvent

SETTINGS = ScheduleSettings.from_config({})

#: Real titles from the provider playlist, as they appeared in stream.sources on
#: 2026-08-01 (all of them describe the previous week's card).
LAST_WEEKS_TITLES = [
    "PPV09 | Mixed Martial Arts: UFC Fight Night: Ankalaev vs Guskov July 25 10:00 AM",
    "PPV 01 |   09:00AM UFC FIGHT NIGHT ANKALAEV V GUSKOV (JUL 25)",
    "PPV 17 |   UFC FN ANKALAEV VS. GUSKOV (7.25 11:00 AM ET) UFC",
    "PPV 16 |   PRELIMS UFC FN (7.25 9:00 AM ET) UFC",
]

TONIGHTS_TITLES = [
    "PPV 03 |   UFC FN MEDIC VS. RODRIGUEZ (8.01 10:00 AM ET) UFC",
    "PPV 04 |   PRELIMS UFC FN MEDIC V RODRIGUEZ (8.01 7:00 AM ET) UFC",
]

CARD_DAY = datetime(2026, 8, 1, tzinfo=UTC)


def entry(title, url="https://soursignal.com/1012026505/u43qz5tstn/400422673"):
    return {"title": title, "attrs": {"group-title": "PPV Live Events"}, "url": url}


def build_event(name="UFC Fight Night: Medić vs. Rodriguez", short_name="Medić vs. Rodriguez"):
    """The 2026-08-01 card as ESPN reported it: prelims 14:00Z, main card 17:00Z."""
    return UfcEvent(
        event_id="600059339",
        name=name,
        short_name=short_name,
        venue="UFC APEX",
        city="Las Vegas, USA",
        cards=(
            CardSegment(start=CARD_DAY.replace(hour=14), label="Prelims", bout_count=6, completed_bouts=0),
            CardSegment(start=CARD_DAY.replace(hour=17), label="Main card", bout_count=5, completed_bouts=0),
        ),
        is_final=False,
        main_event_bout="Ihor Medić vs. Gilbert Rodriguez",
        fighters=("Ihor Medić", "Gilbert Rodriguez"),
    )


def context_at(moment):
    return build_event().context(moment, SETTINGS)


def private_config(**overrides):
    return {**app.DEFAULT_CONFIG["private_iptv"], "enabled": True, **overrides}


# --- match terms -------------------------------------------------------------
def test_match_terms_are_the_fighters_not_the_boilerplate():
    terms = build_event().match_terms()

    assert "medic" in terms
    assert "rodriguez" in terms
    # Every candidate carries these, so they identify nothing.
    for noise in ("ufc", "fight", "night", "vs"):
        assert noise not in terms


def test_accented_names_match_an_ascii_provider_title():
    """ESPN says "Medić"; the provider says "MEDIC". Without folding, the gate
    rejects every feed and the card goes dark."""
    matched, hits = context_at(CARD_DAY).matches("PPV 03 | UFC FN MEDIC VS. RODRIGUEZ (8.01 10:00 AM ET)")

    assert matched
    assert "medic" in hits


def test_numbered_cards_match_on_the_event_number():
    event = build_event(name="UFC 330: Makhachev vs. Machado Garry", short_name="Makhachev vs. Machado Garry")
    context = event.context(CARD_DAY, SETTINGS)

    matched, hits = context.matches("PPV 01 | UFC 330 MAIN CARD")

    assert matched
    assert "ufc 330" in hits


# --- the anchor case ---------------------------------------------------------
def test_last_weeks_channels_are_rejected_for_tonights_card():
    """The 2026-08-01 regression, in full."""
    context = context_at(CARD_DAY.replace(hour=13, minute=45))
    entries = [entry(title) for title in LAST_WEEKS_TITLES]
    rejected = []

    selected = app.select_private_iptv_candidates(
        entries,
        private_config(),
        now=CARD_DAY.replace(hour=6, minute=45),
        context=context,
        rejected=rejected,
    )

    assert selected == []
    assert rejected, "a near miss must be reported so the cockpit can explain the hold"
    assert "Medi" in rejected[0]["reason"]


def test_tonights_channels_are_selected_and_outrank_generic_slots():
    context = context_at(CARD_DAY.replace(hour=13, minute=45))
    entries = [
        *[entry(title) for title in LAST_WEEKS_TITLES],
        *[entry(title, url=f"https://soursignal.com/x/y/{index}") for index, title in enumerate(TONIGHTS_TITLES)],
    ]

    selected = app.select_private_iptv_candidates(
        entries,
        private_config(),
        now=CARD_DAY.replace(hour=6, minute=45),
        context=context,
    )

    titles = [item["entry"]["title"] for item in selected]
    assert titles, "tonight's feeds must survive the gate"
    assert all("ANKALAEV" not in title for title in titles)
    assert any("event match" in reason for reason in selected[0]["reasons"])


def test_a_generic_slot_dated_today_still_qualifies():
    """Many provider event slots carry no names at all — only a date and a phase."""
    context = context_at(CARD_DAY.replace(hour=13, minute=45))

    selected = app.select_private_iptv_candidates(
        [entry("PPV 07 |   UFC MAIN CARD (8.01 10:00 AM ET)")],
        private_config(),
        now=CARD_DAY.replace(hour=6, minute=45),
        context=context,
    )

    assert len(selected) == 1


def test_without_a_tracked_card_the_old_behaviour_is_unchanged():
    """The cockpit still works standalone, with no auto-schedule running."""
    selected = app.select_private_iptv_candidates(
        [entry(title) for title in LAST_WEEKS_TITLES],
        private_config(),
        now=datetime(2026, 7, 25, 6, 45, tzinfo=UTC),
        context=None,
    )

    assert selected, "no tracked card means no event gate"


# --- segment awareness -------------------------------------------------------
def test_slot_start_uses_espns_real_segment_times():
    """The hardcoded 5/7/9pm ET phase defaults are wrong for a daytime card."""
    context = context_at(CARD_DAY.replace(hour=13))
    now = CARD_DAY.replace(hour=13)

    prelims = app.infer_private_iptv_slot_start("PRELIMS UFC FN", now, context=context)
    main = app.infer_private_iptv_slot_start("UFC FN MEDIC VS RODRIGUEZ MAIN CARD", now, context=context)

    assert prelims == CARD_DAY.replace(hour=14).astimezone(prelims.tzinfo)
    assert main == CARD_DAY.replace(hour=17).astimezone(main.tzinfo)


def test_sources_from_an_earlier_segment_are_stale_once_the_next_one_opens():
    context = context_at(CARD_DAY.replace(hour=17, minute=5))
    config = {
        "stream": {
            "sources": [
                {
                    "id": "private-iptv-prelims",
                    "url": "https://soursignal.com/x/y/1",
                    "enabled": True,
                    "event_id": "600059339",
                    "discovered_at": int(CARD_DAY.replace(hour=13, minute=50).timestamp() * 1000),
                }
            ]
        }
    }

    assert app.sources_predate_current_segment(config, context, now=CARD_DAY.replace(hour=17, minute=5))
    # Re-discovered after the main card opened: current again.
    config["stream"]["sources"][0]["discovered_at"] = int(CARD_DAY.replace(hour=17, minute=2).timestamp() * 1000)
    assert not app.sources_predate_current_segment(config, context, now=CARD_DAY.replace(hour=17, minute=5))


# --- protection, purge, and arming ------------------------------------------
def test_a_healthy_feed_for_the_wrong_card_is_not_protected():
    """Health alone earned protection before; that is what pinned the wrong feed."""
    config = {"private_iptv": private_config(), "stream": {"sources": []}}
    budget = {"stream_uses_private_slot": True, "health_decision": "healthy"}

    assert app.should_protect_live_private_stream(config, budget, context=None)
    assert not app.should_protect_live_private_stream(
        config,
        budget,
        context=context_at(CARD_DAY.replace(hour=15)),
        mismatch_confirmed=True,
    )


def test_the_probe_budget_releases_the_spare_slot_during_a_card():
    config = {"private_iptv": private_config(), "stream": {"sources": [], "links": []}}
    proc = {"managed": True}

    idle = app.private_probe_budget(config, proc=proc, health_doc={"decision": "healthy"}, context=None)
    live = app.private_probe_budget(
        config,
        proc=proc,
        health_doc={"decision": "healthy"},
        context=context_at(CARD_DAY.replace(hour=15)),
    )

    assert live["in_event_window"] is True
    assert idle["in_event_window"] is False


def test_purge_drops_another_cards_sources_and_clears_a_stale_lock():
    config = {
        "stream": {
            "locked_source_id": "private-iptv-ankalaev",
            "sources": [
                {"id": "private-iptv-ankalaev", "url": "https://soursignal.com/x/y/1", "enabled": True, "event_id": "600059000"},
                {"id": "private-iptv-medic", "url": "https://soursignal.com/x/y/2", "enabled": True, "event_id": "600059339"},
                {"id": "manual-backup", "url": "https://example.com/manual.m3u8", "enabled": True},
            ],
        }
    }

    removed = app.purge_foreign_event_sources(config, "600059339")

    ids = [source["id"] for source in config["stream"]["sources"]]
    assert removed == 1
    assert "private-iptv-ankalaev" not in ids
    # Untagged operator sources are never touched.
    assert "manual-backup" in ids
    assert config["stream"]["locked_source_id"] == ""


def test_arming_holds_rather_than_streaming_an_unverified_feed():
    """No source for tonight means nothing goes on air — deliberately."""
    config = {
        "private_iptv": private_config(),
        "stream": {
            "sources": [
                {"id": "private-iptv-ankalaev", "url": "https://soursignal.com/x/y/1", "enabled": True, "event_id": "600059000"}
            ],
            "links": [],
        },
    }
    context = context_at(CARD_DAY.replace(hour=14))

    links, detail = app.schedule_start_links(config, context)

    assert links == []
    assert "no source verified" in detail


def test_arming_uses_the_public_backup_after_repeated_failures(monkeypatch):
    config = {"private_iptv": private_config(public_fallback_after_attempts=2), "stream": {"sources": [], "links": []}}
    context = context_at(CARD_DAY.replace(hour=14))
    monkeypatch.setattr(app, "current_auto_sources", lambda: ["https://backup.example/stream.m3u8"])
    app.SOURCE_SWITCH_STATE["acquire_attempts"] = 2

    try:
        links, detail = app.schedule_start_links(config, context)
    finally:
        app.SOURCE_SWITCH_STATE["acquire_attempts"] = 0

    assert links == ["https://backup.example/stream.m3u8"]
    assert "public source" in detail


def test_public_generic_backup_waits_until_the_card_is_actually_live(monkeypatch):
    config = {"private_iptv": private_config(public_fallback_after_attempts=2), "stream": {"sources": [], "links": []}}
    context = context_at(CARD_DAY.replace(hour=13, minute=55))
    monkeypatch.setattr(app, "current_auto_sources", lambda: ["https://backup.example/stream.m3u8"])
    app.SOURCE_SWITCH_STATE["acquire_attempts"] = 20

    try:
        links, detail = app.schedule_start_links(config, context)
    finally:
        app.SOURCE_SWITCH_STATE["acquire_attempts"] = 0

    assert links == []
    assert "no source verified" in detail


def test_only_the_current_broadcast_segment_is_sent_to_ffmpeg():
    prelim = "https://soursignal.com/x/y/prelim"
    main = "https://soursignal.com/x/y/main"
    config = {
        "stream": {
            "sources": [
                {
                    "id": "prelim",
                    "url": prelim,
                    "enabled": True,
                    "event_id": "600059339",
                    "segment_label": "Prelims",
                    "match_confidence": "exact",
                    "probe_score": 95,
                },
                {
                    "id": "main",
                    "url": main,
                    "enabled": True,
                    "event_id": "600059339",
                    "segment_label": "Main card",
                    "match_confidence": "exact",
                    "probe_score": 95,
                },
            ]
        }
    }

    prelim_context = context_at(CARD_DAY.replace(hour=15))
    main_context = context_at(CARD_DAY.replace(hour=17))

    assert app.event_source_links(config, "600059339", context=prelim_context, now=CARD_DAY.replace(hour=15)) == [prelim]
    assert app.event_source_links(config, "600059339", context=main_context, now=CARD_DAY.replace(hour=17)) == [main]


def test_high_grade_requires_current_segment_identity_and_deep_probe():
    config = {
        "stream": {
            "sources": [
                {
                    "id": "current",
                    "url": "https://soursignal.com/x/y/current",
                    "enabled": True,
                    "event_id": "600059339",
                    "segment_label": "Prelims",
                    "match_confidence": "exact",
                    "probe_score": 95,
                },
                {
                    "id": "future",
                    "url": "https://soursignal.com/x/y/future",
                    "enabled": True,
                    "event_id": "600059339",
                    "segment_label": "Main card",
                    "match_confidence": "exact",
                    "probe_score": 99,
                },
            ]
        }
    }
    context = context_at(CARD_DAY.replace(hour=15))

    assert app.live_stream_is_high_grade(config, context, now=CARD_DAY.replace(hour=15))
    config["stream"]["sources"][0]["probe_score"] = 40
    assert not app.live_stream_is_high_grade(config, context, now=CARD_DAY.replace(hour=15))


def test_switch_guardrails_stop_the_stream_from_flapping():
    config = {"private_iptv": private_config(switch_cooldown_seconds=300, max_switches_per_card=2)}
    app.reset_switch_state("600059339")

    allowed, _ = app.source_switch_allowed(config)
    assert allowed

    app.record_source_switch()
    blocked, reason = app.source_switch_allowed(config)
    assert not blocked
    assert "cooldown" in reason
    # A confirmed wrong-card feed overrides the cooldown: it is not an
    # improvement, it is a correction.
    forced, _ = app.source_switch_allowed(config, force=True)
    assert forced

    app.SOURCE_SWITCH_STATE["last_switch_at"] = 0.0
    app.record_source_switch()
    app.SOURCE_SWITCH_STATE["last_switch_at"] = 0.0
    spent, reason = app.source_switch_allowed(config)
    assert not spent
    assert "budget" in reason


def test_merged_sources_carry_the_card_they_were_found_for():
    config = {"private_iptv": private_config(), "stream": {"sources": [], "links": []}}
    accepted = [{"entry": entry(TONIGHTS_TITLES[0]), "score": 138, "reasons": ["event match:medic"]}]

    app.merge_private_iptv_sources(config, accepted, context=context_at(CARD_DAY.replace(hour=13)))

    assert config["stream"]["sources"][0]["event_id"] == "600059339"
    assert config["stream"]["sources"][0]["discovered_at"] > 0
    # And the tag survives a normalize round-trip, or the next save erases it.
    reloaded = app.normalize_sources(config["stream"]["sources"])
    assert reloaded[0]["event_id"] == "600059339"


def test_a_live_feed_for_another_card_reads_as_unmatched():
    context = context_at(CARD_DAY.replace(hour=15))
    stale = {"stream": {"sources": [{"id": "s1", "url": "https://soursignal.com/x/y/1", "enabled": True, "event_id": "600059000"}]}}
    fresh = {"stream": {"sources": [{"id": "s2", "url": "https://soursignal.com/x/y/2", "enabled": True, "event_id": "600059339"}]}}

    assert not app.live_stream_is_event_matched(stale, context)
    assert app.live_stream_is_event_matched(fresh, context)
    # With no card tracked there is nothing to contradict.
    assert app.live_stream_is_event_matched(stale, None)


def test_stand_down_retires_the_cards_feeds():
    config = {
        "private_iptv": private_config(),
        "stream": {
            "sources": [{"id": "private-iptv-medic", "url": "https://soursignal.com/x/y/2", "enabled": True, "event_id": "600059339"}],
            "links": [],
        },
    }

    assert app.disable_private_iptv_sources(config)
    assert config["stream"]["sources"][0]["enabled"] is False


def test_a_live_card_keeps_its_feeds_through_a_provider_blip():
    """A failed sweep mid-event must not empty the pool under a working encode."""
    config = {
        "private_iptv": private_config(),
        "stream": {
            "sources": [{"id": "private-iptv-medic", "url": "https://soursignal.com/x/y/2", "enabled": True, "event_id": "600059339"}],
            "links": [],
        },
    }

    assert not app.disable_private_iptv_sources(config, keep_event_id="600059339")
    assert config["stream"]["sources"][0]["enabled"] is True


def test_the_card_is_stood_down_when_espn_never_marks_it_final():
    from obbyschedule import ScheduleState, UfcScheduler

    event = build_event()
    state = ScheduleState(
        current_event_id=event.event_id,
        started_by_scheduler=True,
        progress_signature=event.progress_signature,
        progress_seen_at=(CARD_DAY.replace(hour=18)).timestamp(),
    )

    # Two hours after the main card opened, with fights still landing: keep going.
    assert not UfcScheduler.card_stalled(CARD_DAY.replace(hour=19), event, state, SETTINGS)
    # Seven hours in, and nothing has been decided for an hour: it is over.
    assert UfcScheduler.card_stalled(CARD_DAY + timedelta(hours=24), event, state, SETTINGS)
