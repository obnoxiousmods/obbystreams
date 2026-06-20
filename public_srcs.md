# Public Stream Sources

These are public watcher sources for `fight.nswfiles.com`. They are not official
ffmpeg inputs, not `soursignal.com` recovery targets, and not the `.m3u8` output
served by `live.obnoxious.lol` or `s.obby.ca`.

Manage them in the `s.obby.ca` cockpit under `Public Streams`. The backend stores
them in top-level `public_sources` and exposes browser-safe playback URLs through
`/api/public-streams`. Watcher clients must play `playback_url`, not the raw
third-party URL, because these sources may require request headers and may fail
direct browser CORS.

## Current Public Sources

### Phantemlis Premium 86

URL:

```text
https://fomis.phantemlis.top/premium86/tracks-v1a1/mono.m3u8?md5=cP1qktYDQ98gHsa8aGRO7Q&expires=1781995963
```

Required headers:

```text
Referer: https://donis.jimpenopisonline.online/
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36
DNT: 1
```

Notes: returns an HLS media playlist with absolute signed segment URLs.

### Mainstreams Zeuryuegn48

URL:

```text
https://mainstreams.pro/hls/zeuryuegn48.m3u8?st=6x23FeAVnFjHyiaqHuPPwXvIjaNhzV70YuQsuGkCcsA&e=1782003250
```

Required headers:

```text
Referer: https://streams.center/
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36
DNT: 1
```

Notes: returns an HLS media playlist with relative `.ts` segment paths. The proxy
rewrites those relative segment paths back through `/api/proxy-hls`.

### Edgestreams Zeuryuegn48

URL:

```text
https://edgestreams.pro/hls/zeuryuegn48.m3u8?st=KOxI5qxuC8cx_GAItQzVqEn9r-QENZLNbojLenFZjIQ&e=1782003298
```

Required headers:

```text
Referer: https://streams.center/
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36
DNT: 1
```

Notes: same shape as the mainstreams source, useful as a separate public fallback.

### Hereisman 52297

URL:

```text
https://chatgpt.hereisman.net/playlist/52297/load-playlist
```

Required headers:

```text
Accept: */*
Accept-Language: en-US,en;q=0.7
Cache-Control: no-cache
DNT: 1
Origin: https://gooz.aapmains.net
Pragma: no-cache
Referer: https://gooz.aapmains.net/
Sec-Fetch-Dest: empty
Sec-Fetch-Mode: cors
Sec-Fetch-Site: cross-site
Sec-GPC: 1
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36
```

Notes: returns a master playlist. The first variant currently points at another
playlist URL on `pl.goozekhar2.space`. The HLS proxy rewrites that nested playlist
URL, and then rewrites its media segments, so the browser still only talks to
`s.obby.ca`.

## Verification

Use these checks after deployment:

```bash
curl -fsS https://s.obby.ca/api/public-streams
curl -I 'https://s.obby.ca/api/proxy-hls?url=URL_ENCODED_PUBLIC_SOURCE'
curl -fsS https://fight.nswfiles.com/
```

Expected behavior:

- `/api/public-streams` returns these entries with `playback_url`.
- Public playback uses `/api/proxy-hls`, never raw frontend requests to the
  third-party source.
- Official source switching/recovery remains limited to `stream.sources`.
