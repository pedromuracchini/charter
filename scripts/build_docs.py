"""Assemble `docs/` from the repository's existing markdown and the package API.

README/CLAUDE/CONTRIBUTING/SECURITY/CHANGELOG are the source of truth and are
read far more often in the repo than on a site. Copying them at build time
keeps the published docs from drifting, at the cost of those pages being
generated rather than edited.

The API reference is generated too, but only as *stubs*: each page is a
one-line ``::: tollgate.<module>`` directive that mkdocstrings expands from the
real docstrings at build time. Nothing here duplicates prose that lives in the
source.

    uv run --group docs python scripts/build_docs.py
    uv run --group docs mkdocs build --strict
    uv run --group docs mkdocs serve

**This script only removes files it generated itself** — tracked in
``docs/.generated-pages`` — never the whole ``docs/`` tree. Hand-authored pages
can therefore be dropped alongside the generated ones without being deleted on
the next build.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
REFERENCE = DOCS / "reference"

#: Records every path this script wrote on the previous run, so a stale page (a
#: renamed module, a dropped source file) is cleaned up without the script ever
#: having to delete something it did not create.
MANIFEST = DOCS / ".generated-pages"

#: source file -> published page
PAGES = {
    "README.md": "index.md",
    "CLAUDE.md": "architecture.md",
    "CONTRIBUTING.md": "contributing.md",
    "SECURITY.md": "security.md",
    "CHANGELOG.md": "changelog.md",
}

REPO = "https://github.com/tollgate-dev/tollgate/blob/main"

#: module -> page title, for the mkdocstrings reference stubs. The `Reference`
#: nav section in `mkdocs.yml` mirrors this list; keep the two in step.
REFERENCE_MODULES = [
    ("tollgate", "Package API"),
    ("tollgate.core.context", "GuardContext"),
    ("tollgate.core.decorator", "guard"),
    ("tollgate.core.interceptor", "TollgateInterceptor"),
    ("tollgate.core.policy_set", "Policy and PolicySet"),
    ("tollgate.core.reversible", "ReversibleAction"),
    ("tollgate.core.escalation", "Escalation interface"),
    ("tollgate.decisions", "Decisions"),
    ("tollgate.state", "CallState"),
    ("tollgate.redaction", "Redaction"),
    ("tollgate.policies", "Policy library"),
    ("tollgate.escalation", "Escalation handlers"),
    ("tollgate.adapters", "Framework adapters"),
    ("tollgate.multiagent", "Multi-agent"),
    ("tollgate.ledger", "Ledger"),
    ("tollgate.report", "Reports"),
    ("tollgate.linter", "Linter"),
    ("tollgate.otel", "OpenTelemetry"),
    ("tollgate.testing", "Testing utilities"),
]

#: Matches a markdown link target — the `foo.md` in `[text](foo.md)`, with an
#: optional `#anchor`. Deliberately narrow: the previous implementation
#: replaced bare strings like "`CLAUDE.md`" anywhere in the text, which
#: rewrote prose inside historical CHANGELOG entries that were *describing* the
#: file rather than linking to it.
_LINK = re.compile(r"\]\((?P<target>[^)\s#]+)(?P<anchor>#[^)\s]*)?\)")

_ABSOLUTE = ("http://", "https://", "mailto:", "/")


def _rewrite_link(match: re.Match[str]) -> str:
    """Point a repo-relative link at its published page, or back at GitHub."""
    target = match.group("target")
    anchor = match.group("anchor") or ""
    if target.startswith(_ABSOLUTE):
        return match.group(0)
    if target in PAGES:
        return f"]({PAGES[target]}{anchor})"
    # No published page: link to the file in the repository, rather than leave
    # a relative path that resolves to nothing on the site.
    return f"]({REPO}/{target}{anchor})"


def _write(path: Path, text: str, written: list[Path]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    written.append(path)


def _clean_previous() -> None:
    """Delete only the pages a previous run of this script created."""
    if not MANIFEST.exists():
        return
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        stale = ROOT / line
        if stale.is_file():
            stale.unlink()
    MANIFEST.unlink()


def _copy_markdown(written: list[Path]) -> None:
    for source_name, page_name in PAGES.items():
        text = (ROOT / source_name).read_text(encoding="utf-8")
        _write(DOCS / page_name, _LINK.sub(_rewrite_link, text), written)
        print(f"{source_name} -> docs/{page_name}")


def _build_reference(written: list[Path]) -> None:
    lines = [
        "# API Reference",
        "",
        "Generated from the package's own docstrings. The architecture guide",
        "explains *why* these pieces are shaped the way they are; this section",
        "is the signature-level detail.",
        "",
    ]
    for module, title in REFERENCE_MODULES:
        page = f"{module.replace('.', '/')}.md"
        _write(REFERENCE / page, f"# {title}\n\n::: {module}\n", written)
        lines.append(f"- [{title}]({page}) — `{module}`")
    lines.append("")
    _write(REFERENCE / "index.md", "\n".join(lines), written)
    print(f"{len(REFERENCE_MODULES)} reference stubs -> docs/reference/")


def main() -> None:
    _clean_previous()
    DOCS.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    _copy_markdown(written)
    _build_reference(written)
    MANIFEST.write_text(
        "".join(f"{p.relative_to(ROOT).as_posix()}\n" for p in written),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
