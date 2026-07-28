---
layout: page
title: Persistent Stop &amp; Source Blacklist
description: How the operator master kill switch and the persistent per-source blacklist behave, persist, and gate the scrapers.
---

# Persistent Stop &amp; Source Blacklist

Two operator affordances make the cockpit deterministic: a **Stop that stays
stopped**, and a **blacklist that keeps a bad stream gone**.

## Persistent Stop (master kill switch)

Hitting **Stop** in the cockpit (`POST /api/stream/stop`) does three things:

1. Sets `stream.operator_stopped: true` in the config YAML (persisted).
2. Kills the managed ffmpeg encode.
3. Causes both background scrapers to idle on their next tick.

While `operator_stopped` is true:

- The **watchdog** will not auto-restart the exited process
  (`should_watchdog_restart_exited_process` returns `false`, and `watchdog_loop`
  short-circuits).
- The **private-IPTV loop** skips its refresh, so it cannot re-select sources and
  auto-start ffmpeg.
- The **public auto-scraper** skips discovery (existing red tiles are retained).
- On a full service/host restart, boot honours the persisted flag and stays down.

The stop is cleared **only** by an explicit **Start** (`/api/stream/start`) or
**Restart** (`/api/stream/restart`). No process-lifecycle side effect resets it —
notably, `start_managed_process` no longer touches the desired state, so a
watchdog- or scraper-initiated start can never silently re-arm a stopped stream.

The cockpit header shows a **"STOPPED (manual)"** banner and relabels Start as
**Resume** whenever the flag is set. `GET /api/status` exposes it at
`runtime.operator_stopped` and `config.stream.operator_stopped`.

> `operator_stopped` is managed solely by the start/stop/restart endpoints. Do
> not hand-edit it in the YAML expecting the scrapers to react mid-event; use the
> endpoints so the in-memory desired state stays consistent.

## Source blacklist

`source_blacklist` is a persisted list that permanently blocks a stream so it can
never be re-selected by a scraper or shown to viewers. Each entry:

```yaml
source_blacklist:
  - url: https://example.com/known-bad/live.m3u8
    id: private-iptv-slate-feed
    label: Always-on slate feed
    channel: UFC 24/7
    reason: dead/slate feed
    added_at: 0
```

### Matching

A source matches a blacklist entry by any of:

- **Normalized URL** (scheme+host lowercased, trailing slash dropped).
- **URL without its query string** — so a block survives CDN **token rotation**
  on the same path.
- **id** (exact).
- **channel** or **label** (case-insensitive).

### Where it is enforced (defense in depth)

The blacklist is checked at every point a source can enter or be shown:

| Stage | Function | Effect |
| --- | --- | --- |
| Private-IPTV selection | `select_private_iptv_candidates` | Blocked entries dropped before the expensive ffprobe stage |
| Ingest write-barrier | `merge_private_iptv_sources` | Blocked feeds never written into `stream.sources` |
| Public inventory | `public_stream_inventory` | Blocked manual/auto tiles hidden from viewers |
| Public auto-scrape | `_run_auto_scrape` | Blocked scraped URLs never stored |
| Manual add | `add_public_stream` | Rejects a blacklisted URL with `409` |
| Managed status | `source_statuses` | Blocked ingest sources filtered from the cockpit list |

### Managing it

- **Block**: the **Block** button on any managed source or public tile, or
  `POST /api/blacklist` with any of `{url, id, label, channel, reason}`. Blocking
  also immediately strips matching entries from `public_sources` and
  `stream.sources`.
- **Unblock**: the **Blacklist** panel's Unblock button, or
  `POST /api/blacklist/remove` with `{url}` or `{id}`.
- **List**: `GET /api/blacklist`, or read `config.source_blacklist` from
  `/api/status`.

Because the private-IPTV merge rebuilds the auto list from scratch each cycle,
the blacklist — not a one-off remove — is what makes a block stick.
