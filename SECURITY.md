# Security

Obbystreams is an administrative control plane for a local streaming host. Anyone with dashboard access can start and stop the managed stream process and change stream inputs, so treat the dashboard like production infrastructure rather than a public media page.

## Supported Versions

Only the latest tagged release receives security fixes.

| Version | Supported |
| --- | --- |
| `0.2.x` | Yes |
| `0.1.x` | No |

## Threat Model

Primary sensitive assets:

- Dashboard password and session token.
- ArangoDB application password.
- Stream source links.
- Local HLS output directory.
- Ability to start, stop, and restart the transcoder.
- Recent process logs, which may include upstream URLs or ffmpeg errors.

The dashboard should be reachable only through nginx over HTTPS. The Starlette app should listen on `127.0.0.1` and should not be exposed directly to the internet.

## Hardening Checklist

- Keep `/etc/obbystreams/obbystreams.yaml` owned by the service user or root and mode `640` or tighter.
- Use long random values for `dashboard.password`, `dashboard.session_token`, and `arangodb.password`.
- Run Obbystreams as an unprivileged user.
- Give the service user write access only to the HLS output directory and required app paths.
- Keep ArangoDB bound to localhost or a private network.
- Use the scoped `obbystreams_app` ArangoDB account at runtime.
- Keep nginx as the only public entry point.
- Enable TLS and redirect HTTP to HTTPS.
- Avoid storing real production credentials in screenshots, issues, pull requests, or release notes.
- Rotate the dashboard token after sharing debug logs with anyone outside the operator group.

## Authentication Notes

Authenticated API routes accept either:

- `x-obbystreams-token` header.
- `obbystreams_token` cookie set by `POST /api/auth/login`.

If `dashboard.session_token` is empty, guarded routes are effectively open. Do not run production with an empty token.

`/api/health` is intentionally unauthenticated so local monitoring, uptime probes, and reverse proxy checks can read service readiness.

## Operational Security

The service may kill existing `ufc` or `obbystreams` processes when starting a managed stream. Keep the service account scoped to the streaming host and avoid sharing that account with unrelated workloads.

The HLS proxy serves local files from the configured output directory when present, then falls back to configured upstream HLS URLs. Keep `public_hls_url` and stream links limited to trusted HTTP(S) sources.

## Reporting Vulnerabilities

Use GitHub private security advisories for vulnerabilities:

`https://github.com/obnoxiousmods/obbystreams/security/advisories/new`

Include:

- Affected version or commit.
- Reproduction steps.
- Impact.
- Whether credentials, stream URLs, or logs are included.
- Suggested fix, if known.

Do not open public issues for exploitable vulnerabilities.
