"""Assemble `docs/` from the repository's existing markdown.

README/CLAUDE/CONTRIBUTING/SECURITY/CHANGELOG are the source of truth and are
read far more often in the repo than on a site. Copying them at build time
keeps the published docs from drifting, at the cost of `docs/` being generated
rather than edited — which is why it is gitignored.

    uv run --with mkdocs-material python scripts/build_docs.py
    uv run --with mkdocs-material mkdocs serve
"""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

#: source file -> published page
PAGES = {
    "README.md": "index.md",
    "CLAUDE.md": "architecture.md",
    "CONTRIBUTING.md": "contributing.md",
    "SECURITY.md": "security.md",
    "CHANGELOG.md": "changelog.md",
}

REPO = "https://github.com/tollgate-dev/tollgate/blob/main"

#: Links that make sense in a repo checkout but 404 on the site. Files with a
#: published page get a relative link; everything else points back at GitHub.
REWRITES = {
    "`CLAUDE.md`": "[the architecture guide](architecture.md)",
    "See `CLAUDE.md`": "See [the architecture guide](architecture.md)",
    "see `SECURITY.md`": "see [the security policy](security.md)",
    "`CONTRIBUTING.md`": "[the contributing guide](contributing.md)",
    "](LICENSE)": f"]({REPO}/LICENSE)",
}


def main() -> None:
    if DOCS.exists():
        shutil.rmtree(DOCS)
    DOCS.mkdir()
    for source_name, page_name in PAGES.items():
        text = (ROOT / source_name).read_text(encoding="utf-8")
        for old, new in REWRITES.items():
            text = text.replace(old, new)
        (DOCS / page_name).write_text(text, encoding="utf-8")
        print(f"{source_name} -> docs/{page_name}")


if __name__ == "__main__":
    main()
