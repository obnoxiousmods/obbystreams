# Codex Notes

`s.obby.ca` is the cockpit. `fight.nswfiles.com` is the watcher/client in `/home/joey/obbywatcher`.

When working here, keep the official ffmpeg source and pasted public sources separate. The cockpit owns structured private `stream.sources`, private headers, ffmpeg restarts, sour-signal recovery, health checks, public redacted Server 1 status APIs, and a separate top-level `public_sources` inventory whose playback is exposed through CORS-safe proxy URLs. The watcher owns viewer UX, playback controls, viewer heartbeats, and client-side failover/display.

Use `Switch` semantics for choosing the active source. Do not reintroduce Up/Down ordering controls as the primary workflow.

Before finishing meaningful changes, run:

```sh
uv run python -m py_compile app.py bin/obbystreams
uv run pytest -q
npm run typecheck
npm run build
```

Read `docs/system-design.md` before changing source behavior or watcher-facing APIs.
