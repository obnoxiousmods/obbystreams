# Claude Notes

This repository is Obbystreams, the cockpit for `https://s.obby.ca/`.

The public client/watcher is separate: `/home/joey/obbywatcher`, served at `https://fight.nswfiles.com/`.

Keep these responsibilities separate:

- Obbystreams: private official source config, separate pasted public source inventory, sour-signal recovery, ffmpeg lifecycle, private headers, public-source request headers, health checks, source switching, public redacted Server 1 status APIs, and CORS-safe public source proxying.
- Obbystreams private IPTV automation: authenticated provider playlist parsing, UFC/fight-day scoring, private sour-signal budget-aware playback probing, and official `stream.sources` updates. Sour-signal private URLs allow two concurrent readers; ffmpeg owns the first slot and scheduled automation/source-health checks reserve the spare slot while the stream is healthy. Keep managed ffmpeg live 24/7 by default; inactive automation may disable auto-created candidates but must not stop ffmpeg unless `keep_stream_live_when_inactive` is explicitly false.
- ObbyWatcher: public player UI, separate official/public source buttons, viewer telemetry, client failover, chat and public diagnostics.

Do not expose source headers, private IPTV cookies, playlist keys, or cockpit credentials through public endpoints. Do not treat link order as the cockpit workflow; use direct source switching.

See `docs/system-design.md` for the design spec and verification expectations. See `public_srcs.md` for the current public source URLs, required headers, and nested playlist notes.
