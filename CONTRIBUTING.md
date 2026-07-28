# Contributing to Tollgate

## Development setup

This project uses [uv](https://docs.astral.sh/uv/) for dependency management
— not raw `pip`/`venv`.

```bash
# Every extra, matching what CI installs — the adapter and escalation tests
# exercise the real framework objects, not mocks, so they need these present.
uv sync --extra otel --extra langgraph --extra openai-agents --extra mcp
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

## Commit messages

Commits follow [Conventional Commits](https://www.conventionalcommits.org/):
`<type>(<scope>): <imperative description>`. This is enforced — the `commits`
CI job runs `cz check` over every PR's commit range — because the history is
what `CHANGELOG.md` is generated from.

```
fix(engine): do not invoke escalation handlers outside enforce mode
feat(policies): add a domain allowlist policy
docs: explain the delegation chain convention
```

Types: `feat`, `fix`, `refactor`, `perf`, `test`, `docs`, `build`, `ci`,
`chore`. Scope is the module you touched (`engine`, `interceptor`, `ledger`,
`escalation`, `policies`, `adapters`, `otel`, `cli`, `linter`, `report`,
`state`). Add a `BREAKING CHANGE:` footer whenever observable behavior shifts.

**One logical change per commit.** A commit that fixes two unrelated bugs is
two commits — splitting them afterwards is far more work than separating them
up front.

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

## Releasing

Publishing is tag-driven and runs through
`.github/workflows/release.yml` — no one uploads from a laptop.

1. Move the `## [Unreleased]` entries in `CHANGELOG.md` under the new version
   with today's date.
2. Bump `version` in `pyproject.toml`.
3. Commit (`chore(release): v0.2.0`), tag `v0.2.0`, and push both.

The workflow re-runs the full matrix against the tagged commit, builds, and
**fails if the tag doesn't match `pyproject.toml`'s version** — publishing the
wrong version under the right name cannot be undone on PyPI. Upload uses PyPI
trusted publishing (OIDC), so there is no long-lived API token in repo secrets.

## Reporting bugs / requesting features

Open an issue with a minimal reproduction (a small policy + tool call
snippet is usually enough, given how self-contained `GuardContext`/`Policy`
are).

## Security issues

Do not open a public issue for a security vulnerability — see
`SECURITY.md`.
