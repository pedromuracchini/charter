# Contributing to Tollgate

This project ships a [Code of Conduct](CODE_OF_CONDUCT.md) (Contributor
Covenant 2.1). Participating here — issues, pull requests, discussions — means
agreeing to it. Report unacceptable behavior to bonadioar@gmail.com; report
*security vulnerabilities* through `SECURITY.md` instead.

## Development setup

This project uses [uv](https://docs.astral.sh/uv/) for dependency management
— not raw `pip`/`venv`.

```bash
# Every extra, matching what CI installs — the adapter and escalation tests
# exercise the real framework objects, not mocks, so they need these present.
uv sync --extra all
```

CI also runs the suite with **no extras at all**, because the graceful
degradation paths (`otel/config.py` falling back to no-ops, the adapters
skipping their optional imports) are load-bearing behavior. If a change makes
`import tollgate` require an optional dependency, that job is what catches it.

## Running checks

```bash
uv run pytest -q                                          # tests
uv run pytest --cov=tollgate --cov-report=term-missing     # tests with coverage
uv run pytest tests/core/test_reversible.py::test_permanent_blocks -q  # a single test
uv run ruff check .                                        # lint
uv run ruff check --fix .                                  # autofix
uv run ruff format .                                       # format
uv run mypy src/tollgate                                   # strict type check
```

`ruff check`, `mypy` and `pytest` must pass before a PR is merged; CI runs them
automatically (`.github/workflows/ci.yml`), matrixed across Python 3.11–3.14
and across Linux/macOS/Windows. Coverage is gated at 90% on the main leg.

`ruff format` is configured (`[tool.ruff.format]`) but **not yet gated** — the
tree predates it and reformatting it wholesale would bury every future `git
blame`. Format the code you touch; a repo-wide `ruff format` belongs in its own
commit, after which `ruff format --check .` can join the `lint` job.

## Documentation

`docs/` is assembled from the repository's own markdown plus mkdocstrings API
stubs — most of it is generated, so edit the source (`README.md`, `CLAUDE.md`,
or the docstrings themselves), not the generated page:

```bash
uv run --group docs python scripts/build_docs.py
uv run --group docs mkdocs serve
```

Adding a module to the published API reference means adding it to
`REFERENCE_MODULES` in `scripts/build_docs.py` *and* to the `Reference` nav
section in `mkdocs.yml`.

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
   with today's date. This is not bookkeeping — that section *is* the GitHub
   release body (`scripts/changelog_section.py` extracts it), and the release
   job fails if the tagged version has no section.
2. Bump `version` in `pyproject.toml`.
3. Commit (`chore(release): v0.2.0`), tag `v0.2.0`, and push both.

The workflow re-runs the full matrix against the tagged commit, builds, runs
`twine check` and a clean-environment install of the built wheel, and **fails
if the tag doesn't match `pyproject.toml`'s version** — publishing the wrong
version under the right name cannot be undone on PyPI. Upload uses PyPI trusted
publishing (OIDC), so there is no long-lived API token in repo secrets, and
PEP 740 attestations are attached so consumers can verify the artifacts came
from this workflow.

## Reporting bugs / requesting features

Open an issue with a minimal reproduction (a small policy + tool call
snippet is usually enough, given how self-contained `GuardContext`/`Policy`
are).

## Security issues

Do not open a public issue for a security vulnerability — see
`SECURITY.md`.
