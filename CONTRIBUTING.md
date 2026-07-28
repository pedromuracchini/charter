# Contributing to Tollgate

## Development setup

This project uses [uv](https://docs.astral.sh/uv/) for dependency management
— not raw `pip`/`venv`.

```bash
uv sync --extra otel   # installs the project + dev + otel deps into .venv
```

## Running checks

```bash
uv run pytest -q                                          # tests
uv run pytest --cov=tollgate --cov-report=term-missing     # tests with coverage
uv run pytest tests/core/test_reversible.py::test_permanent_blocks -q  # a single test
uv run ruff check .                                        # lint
uv run ruff check --fix .                                  # autofix
uv run mypy src/tollgate                                   # strict type check
```

All four must pass before a PR is merged; CI runs them automatically
(`.github/workflows/ci.yml`), matrixed across Python 3.11–3.13.

## Making a change

1. Read `CLAUDE.md` first — it documents the architecture (the single
   evaluation engine in `_engine.py`, how ambient identity flows through
   `contextvars`, the `Policy` interface, multi-agent identity model, and
   what's deliberately deferred) and the reasoning behind non-obvious design
   choices. Most changes to core behavior belong in `_engine.py`, not in the
   `@guard`/`TollgateInterceptor` call-sites that funnel through it.
2. Add tests alongside the change — the test tree under `tests/` mirrors
   `src/tollgate/`'s package structure.
3. Update `CLAUDE.md` if the change affects architecture, and `CHANGELOG.md`
   under an `## [Unreleased]` section.
4. Keep `ruff`/`mypy --strict` clean; both are treated as build failures.

## Reporting bugs / requesting features

Open an issue with a minimal reproduction (a small policy + tool call
snippet is usually enough, given how self-contained `GuardContext`/`Policy`
are).

## Security issues

Do not open a public issue for a security vulnerability — see
`SECURITY.md`.
