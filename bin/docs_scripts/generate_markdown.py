"""Generate the SDK reference Markdown from source with griffe2md, which never imports the SDK.

A `posthog-capability: <slug>` docstring line puts a symbol under that PostHog/sdk-specs
capability, in the sdk-specs README's order. Untagged symbols go to SDK utils.

    python bin/docs_scripts/generate_markdown.py --sdk-specs DIR [--major 8]
"""

import argparse
import logging
import re
from pathlib import Path

import griffe
import griffe2md

ROOT = Path(__file__).resolve().parents[2]
TYPES = """args.OptionalCaptureArgs feature_flag_evaluations.FeatureFlagEvaluations types.FeatureFlagResult
types.FeatureFlag types.FlagsAndPayloads types.UnresolvedFlagReason feature_flags.InconclusiveMatchError
feature_flags.RequiresServerEvaluation capture_send.CaptureError tracing.span.Span emails.Emails
emails.EmailError emails.SendEmailResult""".split()
UTILS = """integrations.django.PosthogContextMiddleware integrations.flask.PosthogFlaskIntegration
integrations.asgi.PosthogASGIMiddleware integrations.celery.PosthogCeleryIntegration
request.set_socket_options request.enable_keep_alive request.disable_connection_reuse""".split()
TAG = re.compile(r"^[ \t]*posthog-capability:[ \t]*([a-z0-9-]+)[ \t]*$\n?", re.M)
ROLE = re.compile(
    r":(?:py:)?(?:meth|func|class|attr|obj|data|exc|mod|const):`~?([^`]+)`"
)
CONFIG = {
    **griffe2md.default_config,
    "docstring_section_style": "table",
    "show_root_full_path": False,
    "show_signature_annotations": True,
    "separate_signature": True,
    "heading_level": 3,
}


def tag(obj) -> str | None:
    """The capability tag. It and any Sphinx roles are stripped from the docstrings rendered."""
    found = TAG.search(obj.docstring.value) if obj.docstring else None
    for o in [obj, *(m for m in obj.members.values() if not m.is_alias)]:
        if o.docstring:
            text = ROLE.sub(r"`\1`", TAG.sub("", o.docstring.value))
            o.docstring.value = re.sub(r"(?<!`)``([^`]+)``(?!`)", r"`\1`", text)
    return found.group(1) if found else None


def entry(obj, name: str) -> str:
    md = griffe2md.render_object_docs(obj, CONFIG)
    # Annotation links point at anchors this page doesn't have.
    md = re.sub(r"\[([^\]]+)\]\(#[\w.]+\)", r"\1", md)
    # A parameter documented only in the docstring, like a **kwargs key, isn't required.
    md = re.sub(r"^(`\w+` \|  \| .*) \| \*required\*$", r"\1 | ", md, flags=re.M)
    return re.sub(r"^### .*$", f"### `{name}`", md.strip(), count=1, flags=re.M)


def methods(cls) -> list:
    """Public methods and properties, and `__init__`."""
    return [
        m
        for n, m in cls.members.items()
        if not m.is_alias
        and (m.is_function or "property" in m.labels)
        and (not n.startswith("_") or n == "__init__")
    ]


def label(prefix: str, m) -> str:
    return f"{prefix}(...)" if m.name == "__init__" else f"{prefix}.{m.name}"


def capabilities(checkout: str) -> dict[str, str]:
    """Each sdk-specs capability's slug and title in sentence case, in the README's order."""
    readme = (Path(checkout) / "README.md").read_text()
    rows = re.findall(
        r"^\| ([^|]+?) \| \[`openspec/specs/([a-z0-9-]+)/spec\.md`\]", readme, re.M
    )
    acronyms = {"id": "ID", "ai": "AI", "http": "HTTP", "mcp": "MCP"}
    titles = {
        s: " ".join(acronyms.get(w.lower(), w.lower()) for w in n.split())
        for n, s in rows
    }
    return {s: t[0].upper() + t[1:] for s, t in titles.items()}


def has(pkg, path: str) -> bool:
    try:
        pkg[path]
        return True
    except KeyError:
        return False


def reference(pkg, caps: dict[str, str]) -> list[str]:
    order = {slug: i for i, slug in enumerate(caps)}
    utils, types = ([pkg[p] for p in paths if has(pkg, p)] for paths in (UTILS, TYPES))
    client = [(tag(m), m) for m in methods(pkg["client.Client"])]
    asyncs = sorted(
        ((tag(m), m) for m in methods(pkg["async_client.AsyncClient"])),
        key=lambda e: order.get(e[0] or "", len(order)),
    )
    module = [
        m
        for n, m in pkg.members.items()
        if not m.is_alias and m.is_function and not n.startswith("_")
    ]
    for obj in [*module, *utils, *types]:
        tag(obj)
    out = []
    for slug, title in caps.items():
        if tagged := [m for t, m in client if t == slug]:
            out += [f"## {title}", *(entry(m, label("Client", m)) for m in tagged)]
    out += ["## SDK utils", *(entry(m, label("Client", m)) for t, m in client if not t)]
    out += [entry(o, f"posthog.{o.path.removeprefix('posthog.')}") for o in utils]
    out += ["## AsyncClient", "Needs the `async` extra, `posthog[async]`."]
    out += [entry(m, label("AsyncClient", m)) for _, m in asyncs]
    out += ["## Module functions", *(entry(m, f"posthog.{m.name}") for m in module)]
    return out + ["## Types", *(entry(o, o.name) for o in types)]


def front_matter(title: str, sections: list[str]) -> str:
    """The docs site's front matter. Its ToC lists the page's H2 sections only."""
    toc = []
    for heading in [s[3:] for s in sections if s.startswith("## ")]:
        anchor = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        toc.append(f'  - value: "{heading}"\n    url: {anchor}\n    depth: 0')
    head = [f'title: "{title}"', "sidebar: Docs", "showTitle: true"]
    head += [
        "contentMaxWidthClass: max-w-full",
        "platformLogo: python",
        "tableOfContents:",
        *toc,
    ]
    return "\n".join(["---", *head, "---"])


def nest(markdown: str) -> str:
    """Moves each heading down one level and drops the H1. Code stays as written."""
    out, in_fence = [], False
    for line in markdown.strip().split("\n"):
        in_fence ^= bool(re.match(r"\s*```", line))
        if in_fence or not re.match(r"#{1,5} ", line):
            out.append(line)
        elif not line.startswith("# "):
            out.append("#" + line)
    return "\n".join(out)


def write(name: str, sections: list[str]) -> None:
    (ROOT / "references" / name).write_text("\n\n".join(sections) + "\n")
    print(f"wrote references/{name}")


def main() -> None:
    args = argparse.ArgumentParser(description="Generate the SDK reference Markdown.")
    args.add_argument("--sdk-specs", required=True, help="PostHog/sdk-specs checkout")
    args.add_argument(
        "--major", type=int, help="target major, defaults to the declared one"
    )
    opts = args.parse_args()
    logging.getLogger("griffe").setLevel(logging.ERROR)
    pkg = griffe.load(
        "posthog",
        search_paths=[str(ROOT)],
        docstring_parser="google",
        resolve_aliases=False,
    )
    declared = int(
        (ROOT / "posthog/version.py").read_text().split('"')[1].split(".")[0]
    )
    major = opts.major or declared
    started = nest((ROOT / "docs/getting-started.md").read_text())
    body = [
        "## Getting started",
        started,
        *reference(pkg, capabilities(opts.sdk_specs)),
    ]
    title = f"PostHog Python SDK {major}.x" + (
        "" if declared >= major else " (experimental)"
    )
    write(f"posthog-python-{major}.md", [front_matter(title, body), *body])
    if declared >= major:
        write(
            "posthog-python-reference.md",
            [front_matter("PostHog Python SDK", body), *body],
        )
    if guides := sorted((ROOT / "docs").glob("migration-*.md"), reverse=True):
        head = "---\ntitle: PostHog Python SDK migration guides\nsidebar: Docs\nshowTitle: true\n---"
        write(
            "posthog-python-migration.md",
            [head, *(g.read_text().strip() for g in guides)],
        )


if __name__ == "__main__":
    main()
