## What and why

<!-- What changes, and what problem it solves. Link an issue if there is one. -->

## Checklist

- [ ] Commits follow [Conventional Commits](https://www.conventionalcommits.org/)
      (`fix(engine): ...`), one logical change each. CI checks this.
- [ ] `uv run ruff check .`, `uv run mypy src/charter` and `uv run pytest -q` all pass.
- [ ] Behavior changes are covered by a test that **fails without the change**.
- [ ] `CHANGELOG.md` `[Unreleased]` updated.
- [ ] `CLAUDE.md` updated if this changes architecture or a documented invariant.

## Notes for the reviewer

<!-- Anything non-obvious: a tradeoff you made, an alternative you rejected,
     a limitation you're knowingly shipping. -->
