# Codex Notes

`s.obby.ca` is the cockpit. `fight.nswfiles.com` is the watcher/client in `/home/joey/obbywatcher`.

When working here, keep the official ffmpeg source and pasted public sources separate. The cockpit owns structured private `stream.sources`, private headers, ffmpeg restarts, sour-signal recovery, health checks, public redacted Server 1 status APIs, and a separate top-level `public_sources` inventory whose playback is exposed through CORS-safe proxy URLs. Public source records may need their own `headers`; preserve them and read `public_srcs.md` before editing source inventory. The watcher owns viewer UX, playback controls, viewer heartbeats, and client-side failover/display.

Private IPTV automation is part of the official-source lane. It may read an authenticated provider playlist, select UFC/fight-day rows, probe playback only when the private sour-signal connection budget allows it, and update `stream.sources`; it must not write those private entries to `public_sources`. Sour-signal private URLs allow two concurrent readers, so the managed ffmpeg stream owns the first slot and scheduled automation/source-health checks reserve the spare slot while the stream is healthy. Keep the official managed stream live 24/7 by default; inactive automation may disable auto-created candidates but must not stop ffmpeg unless `keep_stream_live_when_inactive` is explicitly false. Keep live provider cookies out of commits and redact cookie/token-like headers from APIs.

Use `Switch` semantics for choosing the active source. Do not reintroduce Up/Down ordering controls as the primary workflow.

Before finishing meaningful changes, run:

```sh
uv run python -m py_compile app.py bin/obbystreams
uv run pytest -q
npm run typecheck
npm run build
```

Read `docs/system-design.md` before changing source behavior or watcher-facing APIs.
