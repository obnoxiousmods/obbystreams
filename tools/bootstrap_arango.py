#!/usr/bin/env python3
"""Create the Obbystreams ArangoDB database, scoped user, and collections."""

import argparse
import base64
import json
import sys
import urllib.error
import urllib.request


def request(method, url, username, password, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    headers["Authorization"] = f"Basic {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=8) as res:
            body = res.read().decode()
            return res.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"raw": body}
        return exc.code, parsed


def main():
    parser = argparse.ArgumentParser(description="Bootstrap ArangoDB for Obbystreams.")
    parser.add_argument("--url", default="http://127.0.0.1:8529")
    parser.add_argument("--root-user", default="root")
    parser.add_argument("--root-password", required=True)
    parser.add_argument("--database", default="obbystreams")
    parser.add_argument("--app-user", default="obbystreams_app")
    parser.add_argument("--app-password", required=True)
    parser.add_argument("--collection", action="append", default=["events", "links", "metrics", "configs", "snapshots"])
    args = parser.parse_args()

    base = args.url.rstrip("/")
    status, body = request("POST", f"{base}/_api/database", args.root_user, args.root_password, {
        "name": args.database,
        "users": [{"username": args.app_user, "passwd": args.app_password, "active": True}],
    })
    if status not in (200, 201, 409):
        print(f"database create failed: HTTP {status} {body}", file=sys.stderr)
        return 1
    print(f"database {args.database}: HTTP {status}")

    for name in args.collection:
        status, body = request("POST", f"{base}/_db/{args.database}/_api/collection", args.app_user, args.app_password, {"name": name})
        if status not in (200, 201, 409):
            print(f"collection {name} failed: HTTP {status} {body}", file=sys.stderr)
            return 1
        print(f"collection {name}: HTTP {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
