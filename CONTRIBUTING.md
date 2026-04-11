# Contributing

## Development

```bash
uv sync --dev
export OBBYSTREAMS_CONFIG=examples/obbystreams.example.yaml
uv run uvicorn app:app --reload --host 127.0.0.1 --port 8767
```

## Checks

```bash
uv run ruff check .
uv run mypy app.py
uv run python -m py_compile app.py tools/bootstrap_arango.py
```

## Pull Requests

- Keep runtime secrets out of commits.
- Include config or docs updates when behavior changes.
- Prefer small PRs with direct validation notes.
