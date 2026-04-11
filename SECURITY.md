# Security

Obbystreams can start and stop a local stream process. Treat the dashboard as an administrative control plane.

## Recommendations

- Keep `/etc/obbystreams/obbystreams.yaml` readable only by the service user/group.
- Use a strong dashboard password and session token.
- Run the service on `127.0.0.1` behind nginx.
- Do not expose ArangoDB to the public internet.
- Use the scoped `obbystreams_app` ArangoDB account for runtime access.

## Reporting

Open a private advisory or contact the repository owner directly for vulnerabilities.
