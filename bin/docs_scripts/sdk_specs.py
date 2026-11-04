"""Read sdk-specs capabilities for the reference: each slug's title, in the README table's order.

The slug is the folder name under `openspec/specs/`, and it becomes the capability's anchor on
the reference page. Pass a checkout of PostHog/sdk-specs at the commit to document against.
"""

import re
import subprocess
from pathlib import Path

ACRONYMS = {"id": "ID", "ai": "AI", "http": "HTTP", "mcp": "MCP"}


def sentence_case(name: str) -> str:
    words = [
        ACRONYMS.get(w.lower(), w if w.isupper() and len(w) > 1 else w.lower())
        for w in name.split()
    ]
    words[0] = words[0][0].upper() + words[0][1:]
    return " ".join(words)


def capabilities(checkout: str) -> dict:
    """The renderer's capability index. It fails when a spec folder has no README row."""
    root = Path(checkout)
    readme = (root / "README.md").read_text()
    rows = re.findall(
        r"^\| ([^|]+?) \| \[`openspec/specs/([a-z0-9-]+)/spec\.md`\]", readme, re.M
    )
    if not rows:
        raise SystemExit(f"{root / 'README.md'} has no capability table")
    caps = {slug: {"title": sentence_case(name)} for name, slug in rows}
    folders = {p.name for p in (root / "openspec/specs").iterdir() if p.is_dir()}
    if folders - set(caps):
        raise SystemExit(
            f"sdk-specs capabilities missing from its README table: {sorted(folders - set(caps))}"
        )
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
    )
    return {"sdkSpecs": head.stdout.strip(), "areas": [], "capabilities": caps}
