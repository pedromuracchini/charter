"""Print the CHANGELOG.md section for one version, for use as release notes.

`gh release create --generate-notes` produces a raw list of commit subjects.
This project already hand-writes a Keep a Changelog entry per release and
enforces Conventional Commits in order to produce it, so the release body
should be that entry — not a worse restatement of it.

    python scripts/changelog_section.py 0.2.0

Exits non-zero if the version has no section, which fails the release job
rather than publishing a release with an empty or wrong body.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"

#: A Keep a Changelog version heading: `## [0.2.0] - 2026-07-28`, `## [0.2.0]`,
#: or `## 0.2.0`. The link-reference brackets and the date are both optional
#: because this file has used more than one of those shapes.
_HEADING = re.compile(r"^##\s+\[?(?P<version>[^]\s]+)]?(?:\s+-\s+(?P<date>\S+))?\s*$")


def section_for(version: str, text: str) -> str | None:
    """Return the body of `version`'s section, or None if there isn't one."""
    lines = text.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        match = _HEADING.match(line)
        if match is None:
            continue
        if start is not None:
            # The next version heading ends the section we were collecting.
            return "\n".join(lines[start:index]).strip()
        if match.group("version") == version:
            start = index + 1
    if start is None:
        return None
    return "\n".join(lines[start:]).strip()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {Path(argv[0]).name} VERSION", file=sys.stderr)
        return 2
    version = argv[1]
    body = section_for(version, CHANGELOG.read_text(encoding="utf-8"))
    if not body:
        print(
            f"no CHANGELOG.md section for version {version!r} — "
            f"add one before tagging (see the release steps in CONTRIBUTING.md)",
            file=sys.stderr,
        )
        return 1
    print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
