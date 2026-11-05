"""Generate the SDK reference Markdown from source with griffe2md, which never imports the SDK.

docs/capabilities.json maps each entry's label to a PostHog/sdk-specs capability, shown in
the sdk-specs README's order. Its `utils` lists the Client members that go to SDK utils, and
its `next` maps labels that only the next major has.

    python bin/docs_scripts/generate_markdown.py --sdk-specs DIR [--major 8]
"""

import argparse
import json
import logging
import re
from pathlib import Path

import griffe
import griffe2md

ROOT = Path(__file__).resolve().parents[2]
TYPES = """args.OptionalCaptureArgs feature_flag_evaluations.FeatureFlagEvaluations types.FeatureFlagResult
types.FeatureFlag types.FlagsAndPayloads types.UnresolvedFlagReason feature_flags.InconclusiveMatchError
feature_flags.RequiresServerEvaluation capture_send.CaptureError tracing.span.Span emails.Emails
emails.EmailError emails.SendEmailResult args.OptionalSetArgs emails.CodeEmailArgs""".split()
UTILS = """integrations.django.PosthogContextMiddleware integrations.flask.PosthogFlaskIntegration
integrations.asgi.PosthogASGIMiddleware integrations.celery.PosthogCeleryIntegration
request.set_socket_options request.enable_keep_alive request.disable_connection_reuse""".split()
FIRST = ["setup", "capture", "identify"]
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


def clean(obj) -> None:
    """Strips Sphinx roles, and the Category section the JSON docs read, from the docstrings."""
    for o in [obj, *(m for m in obj.members.values() if not m.is_alias)]:
        if o.docstring:
            text = ROLE.sub(r"`\1`", o.docstring.value)
            head, _, tail = text.partition("\nCategory:\n")
            text = head.rstrip() + "".join(f"\n{t}" for t in tail.split("\n")[1:])
            o.docstring.value = re.sub(r"(?<!`)``([^`]+)``(?!`)", r"`\1`", text)


def signature(name, function, *_, **__) -> str:
    """griffe2md's signature template gives an unannotated parameter the previous one's
    annotation and drops `async`, so griffe writes the signature instead."""
    prefix = "async " if "async" in function.labels else ""
    return prefix + function.signature(name=str(name).strip())


ENV = griffe2md.prepare_env()
ENV.filters["format_signature"] = signature


def entry(obj, name: str) -> str:
    template = ENV.get_template(f"{obj.kind.value}.md.jinja")
    md = template.render(**griffe2md.prepare_context(obj, CONFIG))
    # Annotation links point at anchors this page doesn't have.
    md = re.sub(r"\[([^\]]+)\]\(#[\w.]+\)", r"\1", md)
    # The site reads Markdown as MDX 1: HTML must be JSX, and braces in <code> are expressions.
    md = re.sub(r"<code>(.*?)</code>", r"`\1`", md.replace("<br>", "<br />"))
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
    # Setup, capture and identify come first; the rest follow the README.
    order = [*FIRST, *(s for s in titles if s not in FIRST)]
    return {s: titles[s][0].upper() + titles[s][1:] for s in order if s in titles}


def has(pkg, path: str) -> bool:
    try:
        pkg[path]
        return True
    except KeyError:
        return False


def reference(pkg, caps: dict[str, str], current: bool) -> list[str]:
    """`current` is false for a next major, which can remove what the map names."""
    order = {slug: i for i, slug in enumerate(caps)}
    utils, types = ([pkg[p] for p in paths if has(pkg, p)] for paths in (UTILS, TYPES))
    module = [
        m
        for n, m in pkg.members.items()
        if not m.is_alias and m.is_function and not n.startswith("_")
    ]
    sync, asyncs = (
        methods(pkg["client.Client"]),
        methods(pkg["async_client.AsyncClient"]),
    )
    found = {label("Client", m) for m in sync} | {
        label("AsyncClient", m) for m in asyncs
    }
    found |= {f"posthog.{m.name}" for m in module}
    for obj in [*sync, *asyncs, *module, *utils, *types]:
        clean(obj)
    data = json.loads((ROOT / "docs/capabilities.json").read_text())
    utils_names, later = set(data.pop("utils")), data.pop("next", {})
    mapped = {**later, **data}
    for name, slug in mapped.items():
        if current and name not in found and name not in later:
            raise SystemExit(f"docs/capabilities.json: no symbol for {name}")
        if slug not in caps:
            raise SystemExit(
                f"docs/capabilities.json: {name}: {slug} isn't in sdk-specs"
            )
    for name in sorted(found - mapped.keys() - utils_names):
        if name.startswith("Client"):
            raise SystemExit(f"docs/capabilities.json: map {name} or add it to utils")
    client = [(mapped.get(label("Client", m)), m) for m in sync]
    asyncs = sorted(
        ((mapped.get(label("AsyncClient", m)), m) for m in asyncs),
        key=lambda e: order.get(e[0] or "", len(order)),
    )
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
        *reference(pkg, capabilities(opts.sdk_specs), declared >= major),
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
