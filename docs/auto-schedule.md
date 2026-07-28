# UFC auto-schedule

Turns the operator **Stop** into a **standby**: the cockpit wakes itself up ~15 minutes before a
UFC card's first segment, follows the card, and stands itself back down once every bout is decided.
Along the way it posts countdown / go-live / wrap-up embeds to Discord.

Motivation: before this, the NVENC transcode ran 24/7 (~39 W) even hours after a card ended, because
the only way to stop it was a manual Stop that stayed down until a human pressed Start.

## Where the code lives

The feature is a self-contained package, `obbyschedule/`, which **never imports `app.py`** — the
cockpit injects its primitives as callbacks, so the dependency runs one way only.

| Module | Contents |
|---|---|
| `models.py` | Frozen dataclasses + `StrEnum`s. `ScheduleSettings.from_config` does all coercion/bounding. No I/O. |
| `espn.py` | `EspnScheduleProvider`. Pure `@staticmethod` parsers (`parse_calendar`, `parse_event`) so tests run on recorded JSON. |
| `notify.py` | `DiscordNotifier` (retries, 429 handling, never raises) + `EmbedBuilder` (dict in, dict out). |
| `state.py` | `ScheduleState` + `ScheduleStateStore` — atomic JSON at `schedule.state_path`. |
| `scheduler.py` | `UfcScheduler`. `decide()` and `due_milestones()` are **pure and synchronous**; `tick()`/`run()` are the async shell. |
| `protocols.py` | `ScheduleProvider` / `Notifier` structural interfaces, so stubs are type-legal. |

## Data source

`https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard` — public, unauthenticated.

* `leagues[0].calendar` → the season's cards (`label`, `startDate`, `endDate`).
* `events[].competitions[]` → one row per bout, each with its own `date` and
  `status.type.completed`. **Distinct dates are the broadcast segments** (early prelims / prelims /
  main card), and the completion flags are how the card's end is detected.

### Gotcha: the calendar start is not always the first bout

For the 2026-07-25 Abu Dhabi card the calendar reads **16:00Z** (main card) while the prelims open at
**13:00Z**. Countdowns therefore anchor on `first_card_start` from the *event detail*, and the detail
is loaded `max(warn_minutes) + 60m + CALENDAR_SKEW(12h)` ≈ 37h ahead so the 24-hour warning is never
missed. `next_wakeup()` keeps that wide window cheap by sleeping until the next milestone rather than
polling every two minutes for a day and a half.

### Gotcha: a scratched bout never completes

`STATUS_CANCELED` / `STATUS_POSTPONED` bouts are excluded from the "is everything done?" test, and a
completed main event also ends the card. Otherwise one cancelled prelim would hold the stream up
forever.

## Policy

`UfcScheduler.decide()` — evaluated fresh every tick (level-triggered, so a restart self-heals):

* **START** once `now >= first_card_start - lead_minutes` (default 15), if the card is not finished,
  not suppressed, and nothing is already running. Clears the operator Stop and kicks a private-IPTV
  refresh — the pre-roll is the one window where the spare provider connection is free.
* **STOP** once every bout is decided **and** `end_grace_minutes` (default 20) has elapsed since the
  card was first observed final, **or** when `max_runtime_hours` (default 8) trips as a failsafe if
  ESPN stalls. The failsafe is checked first so a stuck grace stamp cannot defeat it.
* **Re-arm** if the encode goes down mid-card (a crash the watchdog could not recover, or a scraper
  stand-down). The scheduler is level-triggered, so it re-issues START rather than sitting idle until
  the card ends.
* Only ever stands down a stream it **owns** (`started_by_scheduler`).

### Adoption — why it isn't "only what I started"

The obvious rule, *never touch a stream you did not start*, quietly makes the whole feature a no-op
in the case that actually matters. Production had the encode up for six days straight; if nobody
remembers to press Stop first, the scheduler never owns it, the stand-down never fires, and the card
ends with ffmpeg still burning — the exact 24/7 behaviour this replaces.

So `should_adopt()` takes ownership of an already-running stream, but only once the card is genuinely
in its window (`PRE_ROLL` or `LIVE`). It will **not** adopt a card the operator vetoed with Stop, one
already stood down, a finished card, or anything at all while auto-schedule is off. That keeps the
operator's explicit "not this card" intact while still guaranteeing the card ends in standby.

### Operator override

Pressing **Stop** during a card records `stream.stop_reason = "manual"` and stamps
`suppressed_event_id` with the card being tracked. That means *"not this event"* — the scheduler
stays armed for the next one. A manual **Start** or **Restart** lifts the suppression.

`schedule_start_stream` / `schedule_stop_stream` in `app.py` both take `PROCESS_LOCK`, which is what
serialises the scheduler against a human pressing Stop: whoever takes the lock first wins, and the
loser observes the other's effect.

### Ownership continuity

If ESPN changes an event's id mid-card, `ScheduleState.track()` **carries** scheduler ownership onto
the new id. Dropping it would orphan the encode, since the stand-down branch only fires for
scheduler-owned events.

## Notifications

Milestones per card, each fired at most once (deduped in the persisted ledger):

* **Coming up** — posted when a card is first tracked (~37h out), announcing what's next with a
  static per-timezone time table for every segment. See below.
* Countdowns at **24h / 12h / 6h / 2h / 30m** before the first bout (`notify.warn_minutes`).
* **Card start** for each segment.
* **Event end**, including the main-event winner from `competitors[].winner`.

### "Coming up" and the timezone table

Discord's `<t:…:R>` markup only localises for the person reading the message *in Discord*. That is
useless the moment someone quotes the post, screenshots it, or reads it through a bridge — and this
audience is spread across North America, Europe and APAC. So the "Coming up" embed carries a static,
aligned table per card segment:

```
Los Angeles  Sat 1 Aug, 10:00 AM PDT
New York     Sat 1 Aug, 1:00 PM EDT
London       Sat 1 Aug, 6:00 PM BST
Sydney       Sun 2 Aug, 3:00 AM AEST
```

Zones come from `notify.timezones`, which accepts bare IANA names or `{zone, label}` pairs. An
unknown zone is **dropped**, never rendered: `load_zone()` falls back to UTC, so a typo would
otherwise print a confidently wrong time under the right city name.

Operators can re-post it any time from the cockpit's **Post "Coming up"** button, or
`POST /api/schedule {"coming_up": true}` — that path force-fetches the next card's detail even when
the loop would not have loaded it yet, and bypasses the once-per-event ledger.

Card times use Discord's dynamic timestamp markup (`<t:epoch:f>` / `<t:epoch:R>`) so each reader sees
them in their own timezone. Every embed links to <https://fight.nswfiles.com/>.

A milestone more than `notify.max_late_minutes` (20) past due is **dropped, not fired** — after a
redeploy or an outage the channel should get silence, not a burst of stale countdowns. `sweep_stale()`
marks those as fired so they are not re-evaluated forever.

## Config

Lives under the top-level `schedule:` key.

```yaml
schedule:
  enabled: true
  espn_scoreboard_url: https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard
  include_pattern: '^UFC\s+(\d+|Fight Night)'
  exclude_pattern: 'Contender Series|Road to UFC'
  lead_minutes: 15
  end_grace_minutes: 20
  max_runtime_hours: 8
  calendar_refresh_seconds: 3600
  live_poll_seconds: 120
  display_timezone: Canada/Pacific
  state_path: /etc/obbystreams/schedule_state.json
  notify:
    enabled: true
    discord_webhook_url: '<secret>'
    watcher_url: https://fight.nswfiles.com/
    warn_minutes: [1440, 720, 360, 120, 30]
    notify_card_start: true
    notify_event_end: true
    max_late_minutes: 20
```

> **`normalize_config` gotcha.** It rebuilds the config from `DEFAULT_CONFIG` on every save and only
> copies whitelisted sections. `schedule` is copied through explicitly in `normalize_config`; without
> that line an operator's settings would be silently erased by the next unrelated `save_config()`.
> There is a regression test for exactly this (`test_schedule_section_survives_an_unrelated_save`).

The webhook URL is a bearer credential and is masked to `"***"` by `public_config()`, so it never
leaves the box via `/api/config` or `/api/status`.

## API

* `GET /api/schedule` (guarded) → `{enabled, phase, event, next_event, countdown_seconds, cards, …}`
* `POST /api/schedule` (guarded) → `{"enabled": bool}` to toggle, `{"test_notification": true}` to
  post one test embed.
* The same snapshot is embedded in `/api/status` under `schedule`, so the SPA needs no extra polling.

## Cockpit UI

* The header banner becomes state-aware: **`⏸ STANDBY — auto-starts for … in 3h 12m`** while
  auto-schedule is on, versus the existing red `⏹ STOPPED (manual)` when it is off.
* An **Auto-schedule** switch sits in the header actions.
* A `SchedulePanel` lists the tracked card, each segment's start time, and bout progress, plus a
  **Test Discord** button.
* Pure helpers live in `frontend/src/lib/schedule.ts` so the countdown/banner logic is vitest-testable
  without a DOM.

## Verifying

```bash
cd /opt/obbystreams
uv run ruff check . && uv run ty check && uv run pytest -q && npm test && npm run build

# Walk a whole event timeline offline — no network, no Discord, no ffmpeg:
uv run python tools/schedule_replay.py --step 10

# Against the real ESPN calendar (only meaningful for a card that hasn't happened yet):
uv run python tools/schedule_replay.py --live
```

Tests: `tests/test_schedule_parsing.py` (ESPN shapes), `tests/test_schedule_policy.py` (the state
machine on a fake clock), `tests/test_schedule_notifications.py` (milestones + embeds),
`tests/test_schedule_loop.py` (the async shell with stubs). Fixtures in `tests/fixtures/espn_*.json`
are trimmed captures of the real 2026-07-25 card plus pre / mid / cancelled variants.

Note the test harness never enters the ASGI lifespan, so `SCHEDULER` is `None` there — the endpoints
degrade to an "scheduler not running" snapshot rather than erroring.

## Deploying

Code changes require restarting `obbystreams.service`, which **interrupts the live encode**. Do it
in a non-event window and check `/api/status` for viewers first.
