"""End condition for the Python reference page.

Builds its own inventory of the public Python surface with `ast`, independent of the extractor,
then checks the generated page against it and against the release rules:

    python bin/docs_scripts/check_reference.py <reference.md> <PostHog/sdk-specs checkout>

Exits 1 and lists every failure.
"""

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sdk_specs  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]

STRUCTURE = {"Getting started", "SDK utils", "Configs", "Types"}
BANNED = {
    "Used by section": r"^(#+ |\*\*)?Used by\b[:*]",
    "preview wording": r"\b(preview|candidate)\b",
    "commit SHA": r"\b[0-9a-f]{12,40}\b",
    "empty class stub": r"^class \w+\(\):\s*$",
    "scraped icon text": r"svg/svg",
    "Sphinx role": r":(meth|func|class|attr|obj|data|exc|mod):`",
    "Sphinx literal block": r"::$",
    "variadic default": r"\*\*\w+[^,)\n]* = \{\}",
    "private parameter": r"^- `_\w+`",
    "old example label": r"\*\*(Good|Bad) example",
    "unlabeled example": r"\*\*Example\.\*\*",
    "list instead of table": r"^- `\w+` \(",
}


def public_defs(path: Path, class_name: str | None) -> list[str]:
    tree = ast.parse(path.read_text())
    body = tree.body
    if class_name:
        body = next(
            n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name
        ).body
    names = [
        n.name for n in body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    return sorted(
        {n for n in names if not n.startswith("_")} | ({"__init__"} & set(names))
    )


def slugify(text: str) -> str:
    return re.sub(r"[^\w\- ]", "", text.lower()).replace(" ", "-")


def main(md_path: str, specs: str) -> int:
    root = ROOT
    md = Path(md_path).read_text()
    caps = sdk_specs.capabilities(specs)
    failures = []
    headings = set(re.findall(r"^#{3,4} `([^`]+)`$", md, re.M))

    inventory = {
        "Client": public_defs(root / "posthog/client.py", "Client"),
        "AsyncClient": public_defs(root / "posthog/async_client.py", "AsyncClient"),
        "posthog": public_defs(root / "posthog/__init__.py", None),
    }
    for owner, names in inventory.items():
        for name in names:
            label = (
                f"{owner}({owner == 'posthog' and '' or '...'})"
                if name == "__init__"
                else f"{owner}.{name}"
            )
            if name == "__init__":
                label = f"{owner}(...)"
            if label not in headings:
                failures.append(f"missing public symbol {label}")

    status = re.search(r"^status: (current|experimental)$", md, re.M)
    if not status:
        failures.append("front matter has no current or experimental status")
    h2 = re.findall(r"^## (.+)$", md, re.M)
    allowed = (
        STRUCTURE
        | {c["title"] for c in caps["capabilities"].values()}
        | {"AsyncClient", "Module functions"}
    )
    failures += [f"unexpected top-level heading {h}" for h in h2 if h not in allowed]

    blocks = len(
        re.findall(
            r"^<summary>(Parameters|Fields|Settings) \(\d+\)</summary>$", md, re.M
        )
    )
    if blocks != md.count("<details>"):
        failures.append(
            f"{blocks} member blocks but {md.count('<details>')} details blocks"
        )
    tables = len(
        re.findall(
            r"^<summary>(Parameters|Fields|Settings) \(\d+\)</summary>\n\n\| (Parameter|Field|Setting) \|\n\| --- \|$",
            md,
            re.M,
        )
    )
    if tables != blocks:
        failures.append(
            f"{blocks} member blocks but only {tables} open with a one-column table"
        )
    if re.search(r"<br>", md) or re.search(
        r"^#{3,6} `\w+` (parameter|field|setting)$", md, re.M
    ):
        failures.append("a bare <br> or a member heading is left on the page")
    # Replay the site's heading slugger: every capability heading must keep its plain sdk-specs anchor.
    seen: dict[str, int] = {}
    ids: list[tuple[str, str, str]] = []
    in_fence = False
    for line in md.split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        heading = None if in_fence else re.match(r"^(#{1,6}) (.+)$", line)
        if heading:
            base = slugify(heading.group(2))
            n = seen.get(base, 0)
            seen[base] = n + 1
            ids.append(
                (heading.group(1), heading.group(2), base if n == 0 else f"{base}-{n}")
            )
    for level, text, anchor in ids:
        cap = caps["capabilities"].get(slugify(text))
        if level == "##" and cap and cap["title"] == text and anchor != slugify(text):
            failures.append(f"capability {text!r} lost its anchor, got #{anchor}")
    failures += [
        f"escaped pipe inside code span: {s!r}"
        for s in re.findall(r"`([^`\n]+)`", md)
        if "\\|" in s
    ]
    for block in re.findall(
        r"<summary>Fields \(\d+\)</summary>.*?</details>", md, re.S
    ):
        failures += [
            f"field default is a name, not a value: {d!r}"
            for d in re.findall(r"\*\*Default:\*\* `([^`]+)`", block)
            if re.fullmatch(r"[a-z_][\w.]*( or .+)?", d)
            and d not in ("None", "True", "False")
        ]
    for entry in re.split(r"^(?=#{3,4} `)", md, flags=re.M):
        if (
            entry.startswith(("### `AsyncClient", "#### `AsyncClient"))
            and "**Available in:**" not in entry.split("\n#", 1)[0]
        ):
            failures.append(
                f"{entry.splitlines()[0]} doesn't say it needs the async extra"
            )
    types = md.split("## Types", 1)[-1]
    for name in (
        "CaptureCompression",
        "FeatureFlagEvaluationRuntime",
        "UnresolvedFlagReason",
    ):
        section = types.split(f"### `{name}`", 1)[-1].split("\n### ", 1)[0]
        if f"### `{name}`" in types and "**Value:**" not in section:
            failures.append(f"enum {name} doesn't show its member values")
    failures += [
        f"type method {m} has no description"
        for m in re.findall(
            r"^#### `([^`]+)`\n\n(?:```|<details>|\*\*Returns|\[Source)", types, re.M
        )
    ]
    for block in re.findall(
        r"<summary>Fields \(\d+\)</summary>(.*?)</details>", types, re.S
    ):
        failures += [
            f"type field {r.split('`')[1]} has no description"
            for r in re.findall(r"^\| (`.+) \|$", block, re.M)
            if not re.search(r"<br />(?!\*\*)[^<]", r)
        ]
    prose = re.sub(r"```.*?```", "", md, flags=re.S)
    for name, pattern in BANNED.items():
        for match in re.finditer(pattern, prose, re.M | re.I):
            failures.append(f"{name}: {match.group(0)!r}")

    for failure in failures:
        print(f"FAIL {failure}")
    total = sum(len(v) for v in inventory.values())
    print(
        f"{len(failures)} failures, {total} public symbols checked, {len(h2)} top-level headings"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
