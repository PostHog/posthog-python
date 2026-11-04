"""Generate the SDK reference Markdown from source with Griffe, which never imports the SDK.

A `posthog-capability: <slug>` docstring line puts a symbol under that PostHog/sdk-specs
capability, in the sdk-specs README's order. Untagged symbols go to SDK utils, grouped by
their `Category:`.

    python bin/docs_scripts/generate_markdown.py --sdk-specs DIR [--major 8 --source-ref v1]
"""

import argparse
import logging
import re
import textwrap
from pathlib import Path
from typing import NamedTuple

import griffe

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "[Source](https://github.com/PostHog/posthog-python/blob/{ref}/{path}#L{line})"
GUIDES = "---\ntitle: PostHog Python SDK migration guides\nsidebar: Docs\nshowTitle: true\n---"
TYPES = """args.OptionalCaptureArgs args.OptionalSetArgs feature_flag_evaluations.FeatureFlagEvaluations
types.FeatureFlagResult types.FeatureFlag types.FlagsAndPayloads types.FeatureFlagEvaluationRuntime
types.UnresolvedFlagReason flag_definition_cache.FlagDefinitionCacheProvider flag_definition_cache.FlagDefinitionCacheData
feature_flags.InconclusiveMatchError feature_flags.RequiresServerEvaluation capture_send.CaptureError
capture_compression.CaptureCompression tracing.span.Span
metrics_capture.PostHogMetrics emails.Emails emails.AsyncEmails emails.EmailError emails.EmailStatus
emails.SendEmailResult ai.prompts.Prompts""".split()
UTILS = {
    "Framework integrations": """integrations.django.PosthogContextMiddleware integrations.flask.PosthogFlaskIntegration
        integrations.asgi.PosthogASGIMiddleware integrations.celery.PosthogCeleryIntegration
        integrations.drf.create_exception_handler integrations.otel.PostHogContextSpanProcessor
        integrations.otel.enrich_current_span""".split(),
    "HTTP connection": "request.set_socket_options request.enable_keep_alive request.disable_connection_reuse".split(),
}
WRAPPERS = "openai anthropic gemini langchain openai_agents claude_agent_sdk".split()
CONFIGS = [("integrations.django.PosthogContextMiddleware", "Django settings")]
# Where a symbol works, by module, when that isn't everywhere Python runs.
AVAILABLE_IN = {
    "async_client": "the `async` extra, `posthog[async]`",
    "integrations.django": "Django apps",
    "integrations.drf": "Django REST framework apps",
    "integrations.flask": "Flask apps",
    "integrations.celery": "Celery workers",
    "integrations.asgi": "ASGI apps, such as FastAPI and Starlette",
    "integrations.otel": "the `otel` extra, `posthog[otel]`",
    "ai.langchain": "the `langchain` extra, `posthog[langchain]`",
    "ai.openai": "apps with the `openai` package",
    "ai.anthropic": "apps with the `anthropic` package",
    "ai.gemini": "apps with the Google Gen AI package",
    "ai.openai_agents": "apps with the `openai-agents` package",
    "ai.claude_agent_sdk": "apps with the `claude-agent-sdk` package",
}
ACRONYMS = {"id": "ID", "ai": "AI", "http": "HTTP", "mcp": "MCP"}
STAR = {"variadic positional": "*", "variadic keyword": "**"}
TAG = re.compile(r"^[ \t]*posthog-capability:[ \t]*([a-z0-9-]+)[ \t]*$\n?", re.M)
CAPABILITY_ROW = re.compile(
    r"^\| ([^|]+?) \| \[`openspec/specs/([a-z0-9-]+)/spec\.md`\]", re.M
)
FENCE = re.compile(r"```(\w*)\n(.*?)```", re.S)
LABEL = re.compile(r"#\s*(Correct example|Counter example \(incorrect\))\.?:?\s*(.*)")
ROLE = re.compile(
    r":(?:py:)?(?:meth|func|class|attr|obj|data|exc|mod|const):`~?([^`]+)`"
)
LITERAL = re.compile(r"^([^\n]*?)::\n\n((?:(?:[ \t]+[^\n]*)?\n)+)", re.M)
LITERAL_VALUE = re.compile(
    r"""^(None|True|False|-?\d[\d_.e]*|(["']).*\2|\[\]|\{\}|\(\))$"""
)
SETTING = re.compile(r"^\s*- `(POSTHOG_[A-Z_]+)`: (.*?)(?=^\s*- `|^\s*$)", re.M | re.S)
VALUE = re.compile(r"^(\w+): (.*?)(?=^\w+:|\Z)", re.M | re.S)
NEWLINE = re.compile(r"\s*\n\s*")


class Entry(NamedTuple):
    slug: str | None
    group: str | None
    name: str
    body: list[str]

    def render(self, level: int) -> str:
        return "\n\n".join([f"{'#' * level} `{self.name}`", *self.body])


def text(value) -> str:
    return "" if value is None else str(value)


def get(obj, path: str):
    try:
        return obj[path]
    except KeyError:
        return None


def fence(code: str) -> str:
    return f"```python\n{code.strip()}\n```"


def code(value) -> str:
    return f"`{NEWLINE.sub(' ', str(value)).strip()}`" if value else ""


def md(value: str) -> str:
    """Sphinx roles, double backticks and literal blocks become Markdown."""
    value = re.sub(r"``([^`]+)``", r"`\1`", ROLE.sub(r"`\1`", value or ""))

    def block(m: re.Match) -> str:
        lead = m.group(1) + ":\n\n" if m.group(1).strip() else ""
        return lead + fence(textwrap.dedent(m.group(2))) + "\n\n"

    return LITERAL.sub(block, value + "\n").strip()


def unpipe(span: str) -> str:
    """Escapes pipes for a table. A pipe splits its code span, since MDX shows `\\|` literally."""
    if not span.startswith("`"):
        return span.replace("|", "\\|")
    return " \\| ".join(f"`{x}`" for x in re.split(r"\s*\|\s*", span[1:-1]))


def cell(value) -> str:
    """Table cell text: fences become inline code and paragraphs join with <br />."""
    value = FENCE.sub(
        lambda m: f"`{NEWLINE.sub('; ', m.group(2).strip())}`", text(value).strip()
    )
    value = "<br />".join(NEWLINE.sub(" ", p) for p in re.split(r"\n\s*\n", value))
    return "".join(unpipe(s) for s in re.split(r"(`[^`]*`)", value))


def row(name, desc="", kind="", default="", required=False, value="", where="") -> str:
    """One parameter, field or setting: its name, a labels line, then its description."""
    default = (
        "**Required**"
        if required
        else default and f"**Default:** {cell(code(default))}"
    )
    labels = [
        where and f"**Where:** {where}",
        value and f"**Value:** {cell(code(value))}",
        kind and f"**Type:** {cell(code(kind))}",
        default,
    ]
    parts = [code(name), " · ".join(x for x in labels if x), cell(desc)]
    return "| " + "<br />".join(x for x in parts if x) + " |"


def table(title: str, rows: list[str]) -> list[str]:
    """A details block holding a one-column table."""
    head = f"<summary>{title} ({len(rows)})</summary>\n\n| {title[:-1]} |\n| --- |"
    return ["\n".join(["<details>", head, *rows, "", "</details>"])] if rows else []


def parse(obj) -> tuple[str | None, list]:
    """The symbol's capability tag and its parsed docstring sections."""
    raw = obj.docstring.value if obj.docstring else ""
    tag, body = TAG.search(raw), TAG.sub("", raw).strip()
    sections = griffe.Docstring(body, parent=obj).parse("google") if body else []
    return (tag.group(1) if tag else None), sections


def examples(blocks: str) -> list[str]:
    """Each fenced block, labeled by its first comment line. Unlabeled blocks are correct."""
    out = []
    for _, src in FENCE.findall(blocks):
        head, _, rest = src.lstrip("\n").partition("\n")
        label = "**Correct example.**"
        if m := LABEL.match(head.strip()):
            src, why = rest, m.group(2)
            if m.group(1).startswith("Counter"):
                why = (
                    why[:1].upper()
                    + why[1:]
                    + ("." if why and why[-1] not in ".!?" else "")
                )
                label = f"**Counter example (incorrect).** {why}".strip()
        out.append(f"{label}\n\n{fence(src)}")
    return out


def public(p) -> bool:
    return p.name not in ("self", "cls") and not p.name.startswith("_")


def signature(fn, name: str) -> str:
    parts = []
    for p in filter(public, fn.parameters):
        star = STAR.get(p.kind.value, "")
        part = star + p.name + (f": {p.annotation}" if p.annotation is not None else "")
        parts.append(
            part + (f" = {p.default}" if p.default is not None and not star else "")
        )
    returns = (
        f" -> {fn.returns}" if fn.returns is not None and name[0].islower() else ""
    )
    head = ("async " if "async" in fn.labels else "") + name
    if len(line := f"{head}({', '.join(parts)}){returns}") <= 88:
        return line
    return f"{head}(\n" + "".join(f"    {p},\n" for p in parts) + f"){returns}"


def params(fn, documented: dict) -> list[str]:
    """A row per parameter. Each documented key of an `Unpack[...]` TypedDict gets a row."""
    rows = []
    for p in filter(public, fn.parameters):
        star = STAR.get(p.kind.value, "")
        unpacked = re.fullmatch(r"Unpack\[(\w+)\]", text(p.annotation))
        typed = get(fn.package, f"args.{unpacked.group(1)}") if unpacked else None
        if star == "**" and typed and (fields := typed.attributes):
            for n in [n for n in documented if n in fields]:
                rows.append(row(n, md(documented.pop(n)), text(fields[n].annotation)))
            continue
        desc = md(documented.pop(p.name, ""))
        default, required = (
            "" if star else text(p.default),
            p.default is None and not star,
        )
        rows.append(row(star + p.name, desc, text(p.annotation), default, required))
    return rows + [row(n, md(d)) for n, d in documented.items()]


def entry(obj, name: str, ref: str, group: str | None = None) -> Entry:
    slug, sections = parse(obj)
    texts, notes, shown, raises = [], [], [], []
    documented: dict[str, str] = {}
    returns, deprecated = ("", ""), ""
    for s in sections:
        kind, value = s.kind.value, s.value
        if kind == "text":
            texts.append(value)
        elif kind in ("parameters", "other parameters"):
            documented |= {p.name: p.description for p in value}
        elif kind == "returns":
            rtype = text(value[0].annotation) or text(getattr(obj, "returns", ""))
            returns = (rtype, md(" ".join(r.description for r in value)))
        elif kind == "raises":
            raises = [
                f"**Raises** {code(text(r.annotation))}. {md(r.description)}".strip()
                for r in value
            ]
        elif kind == "examples":
            shown += examples("\n".join(t for _, t in value))
        elif kind == "deprecated":
            deprecated = f"**Deprecated.** {value.description}"
        elif kind == "admonition" and s.title in ("Example", "Examples"):
            body = value.description
            shown += (
                examples(body)
                if "```" in body
                else [f"**Correct example.**\n\n{fence(body)}"]
            )
        elif kind == "admonition" and s.title == "Category":
            group = group or value.description.strip()
        elif kind == "admonition":
            notes.append(f"**{s.title}.** {md(value.description)}")
    sig, rows = "", []
    if obj.is_function:
        sig = signature(
            obj, name.removesuffix("(...)") if obj.name == "__init__" else obj.name
        )
        rows = params(obj, documented)
        if not any(returns) and obj.returns is not None and obj.name != "__init__":
            returns = (text(obj.returns), "")
    elif obj.is_class and "__init__" in obj.members:
        init = obj.members["__init__"]
        args = {
            p.name: p.description
            for s in parse(init)[1]
            if s.kind.value == "parameters"
            for p in s.value
        }
        sig, rows = signature(init, obj.name), params(init, documented | args)
    elif obj.is_attribute and obj.annotation is not None:
        sig = f"{name}: {obj.annotation}"
    where = available_in(obj.module.path)
    returned = any(returns) and f"**Returns** {code(returns[0])}" + (
        returns[1] and f". {returns[1]}"
    )
    body = [
        deprecated,
        where and f"**Available in:** {where}",
        md("\n\n".join(texts)),
        sig and fence(sig),
        *table("Parameters", rows),
        returned and returned.strip(),
        *raises,
        "\n\n".join(notes),
        *shown,
        SOURCE.format(ref=ref, path=obj.relative_package_filepath, line=obj.lineno),
    ]
    return Entry(slug, group, name, [b for b in body if b])


def available_in(module: str) -> str | None:
    module = module.removeprefix("posthog.") + "."
    return next(
        (w for m, w in AVAILABLE_IN.items() if module.startswith(m + ".")), None
    )


def members(cls) -> list:
    return [
        m
        for n, m in cls.members.items()
        if not m.is_alias
        and (not n.startswith("_") or n == "__init__")
        and (m.is_function or "property" in m.labels)
    ]


def type_block(cls, ref: str) -> str:
    """A type's section. Dataclasses, TypedDicts, enums and errors show their fields."""
    sections = parse(cls)[1] + (
        parse(cls.members["__init__"])[1] if "__init__" in cls.members else []
    )
    documented = {
        i.name: i.description
        for s in sections
        if s.kind.value in ("attributes", "parameters")
        for i in s.value
    }
    for s in sections:
        if s.kind.value == "admonition" and s.title == "Values":
            documented |= {
                n: " ".join(d.split()) for n, d in VALUE.findall(s.value.description)
            }
    bases = " ".join(text(b) for b in cls.bases)
    data = any("dataclass" in text(d.value) for d in cls.decorators) or any(
        k in bases for k in ("TypedDict", "Enum", "Exception", "Error")
    )
    rows = []
    for n, a in cls.attributes.items():
        if data and not n.startswith("_") and "property" not in a.labels:
            # Only a literal is a default. A field set from an __init__ argument has none.
            literal = text(a.value) if LITERAL_VALUE.match(text(a.value)) else ""
            desc = md(a.docstring.value if a.docstring else documented.get(n, ""))
            enum = "Enum" in bases
            rows.append(
                row(n, desc, value=literal)
                if enum
                else row(n, desc, text(a.annotation), literal)
            )
    methods = [
        entry(m, f"{cls.name}.{m.name}", ref).render(4)
        for m in members(cls)
        if m.name != "__init__"
    ]
    desc = md("\n\n".join(s.value for s in parse(cls)[1] if s.kind.value == "text"))
    if not (desc or rows or methods):
        return ""
    src = SOURCE.format(ref=ref, path=cls.relative_package_filepath, line=cls.lineno)
    return "\n\n".join(
        [
            f"### `{cls.name}`",
            *filter(None, [desc]),
            *table("Fields", rows),
            *methods,
            src,
        ]
    )


def reference(pkg, caps: dict[str, str], ref: str) -> list[str]:
    """Every capability, SDK utils, AsyncClient, module functions, configs and types."""

    def methods(cls: str, prefix: str) -> list[Entry]:
        names = {"__init__": "(...)"}
        return [
            entry(m, prefix + names.get(m.name, f".{m.name}"), ref)
            for m in members(pkg[cls])
        ]

    client, asyncs = (
        methods("client.Client", "Client"),
        methods("async_client.AsyncClient", "AsyncClient"),
    )
    module = [
        entry(m, f"posthog.{n}", ref)
        for n, m in pkg.members.items()
        if not m.is_alias and m.is_function and not n.startswith("_")
    ]
    for group, paths in UTILS.items():
        for obj, path in [(get(pkg, p), p) for p in paths]:
            if obj is not None:
                client.append(entry(obj, f"posthog.{path}", ref, group))
                if obj.is_class:
                    client += [
                        entry(m, f"{obj.name}.{m.name}", ref, group)
                        for m in members(obj)
                        if m.name != "__init__"
                    ]
    for name in WRAPPERS:
        if (mod := get(pkg, f"ai.{name}")) is not None:
            names = ", ".join(
                f"`{n}`"
                for n in mod.members
                if not n.startswith("_") and n[0].isupper()
            )
            summary = md(mod.docstring.value.split("\n\n")[0]) if mod.docstring else ""
            where = available_in(f"ai.{name}")
            body = [
                where and f"**Available in:** {where}",
                " ".join(filter(None, [summary, f"Exports {names}."])),
            ]
            client.append(
                Entry(
                    None,
                    "AI wrapper clients",
                    f"posthog.ai.{name}",
                    [b for b in body if b],
                )
            )
    if unknown := {e.slug for e in client + asyncs + module if e.slug} - caps.keys():
        raise SystemExit(
            f"posthog-capability tags sdk-specs doesn't define: {sorted(unknown)}"
        )

    # Each capability is titled as in sdk-specs, so its heading's anchor is the capability's slug.
    out = []
    for slug, title in caps.items():
        if tagged := [e for e in client if e.slug == slug]:
            out += [f"## {title}", *(e.render(3) for e in tagged)]
    if utils := [e for e in client if not e.slug]:
        out.append("## SDK utils")
        for group in dict.fromkeys(e.group or "Other" for e in utils):
            out += [
                f"### {group}",
                *(e.render(4) for e in utils if (e.group or "Other") == group),
            ]
    for title, entries in (("AsyncClient", asyncs), ("Module functions", module)):
        ordered = sorted(
            entries, key=lambda e: list(caps).index(e.slug) if e.slug else len(caps)
        )
        out += [f"## {title}", *(e.render(3) for e in ordered)]
    settings = []
    for path, where in CONFIGS:
        cls = get(pkg, path)
        doc = cls.docstring.value if cls and cls.docstring else ""
        settings += [
            row(n, md(" ".join(d.split())), where=where)
            for n, d in SETTING.findall(doc)
        ]
    if settings:
        out += ["## Configs", *table("Settings", settings)]
    if types := [
        t for t in (type_block(pkg[p], ref) for p in TYPES if get(pkg, p)) if t
    ]:
        out += ["## Types", *types]
    return out


def capabilities(checkout: str) -> dict[str, str]:
    """Each sdk-specs capability's slug and title in sentence case, in the README's order."""
    caps = {}
    for name, slug in CAPABILITY_ROW.findall(
        (Path(checkout) / "README.md").read_text()
    ):
        words = [
            ACRONYMS.get(w.lower(), w if w.isupper() and len(w) > 1 else w.lower())
            for w in name.split()
        ]
        title = " ".join(words)
        caps[slug] = title[0].upper() + title[1:]
    return caps


def nest(markdown: str, drop_title: bool = True) -> str:
    """Moves each heading down one level, dropping the H1 when asked. Code stays as written."""
    out, in_fence = [], False
    for line in markdown.strip().split("\n"):
        in_fence ^= bool(re.match(r"\s*(```|~~~)", line))
        if in_fence or not re.match(r"#{1,5} ", line):
            out.append(line)
        elif not (drop_title and line.startswith("# ")):
            out.append("#" + line)
    return "\n".join(out).lstrip("\n")


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


def write(name: str, sections: list[str]) -> None:
    (ROOT / "references" / name).write_text("\n\n".join(sections) + "\n")
    print(f"wrote references/{name}")


def main() -> None:
    args = argparse.ArgumentParser(description="Generate the SDK reference Markdown.")
    args.add_argument("--sdk-specs", required=True, help="PostHog/sdk-specs checkout")
    args.add_argument(
        "--major", type=int, help="target major, defaults to the declared one"
    )
    args.add_argument(
        "--source-ref", help="git ref for source links, defaults to the tag"
    )
    opts = args.parse_args()
    logging.getLogger("griffe").setLevel(logging.ERROR)
    pkg = griffe.load("posthog", search_paths=[str(ROOT)], resolve_aliases=False)
    declared = (ROOT / "posthog/version.py").read_text().split('"')[1]
    major = opts.major or int(declared.split(".")[0])
    # A branch that targets a newer major than it has released documents an experimental one.
    status = "current" if int(declared.split(".")[0]) >= major else "experimental"
    version = declared if status == "current" else "unreleased"
    ref = opts.source_ref or f"v{version}"
    started = nest((ROOT / "docs/getting-started.md").read_text())
    reference_md = reference(pkg, capabilities(opts.sdk_specs), ref)
    body = ["## Getting started", started, *reference_md]
    # Each major gets its own page. The current one is also the latest reference.
    title = f"PostHog Python SDK {major}.x" + (
        "" if status == "current" else " (experimental)"
    )
    write(f"posthog-python-{major}.md", [front_matter(title, body), *body])
    if status == "current":
        write(
            "posthog-python-reference.md",
            [front_matter("PostHog Python SDK", body), *body],
        )
    # Newest guide first, so the migration page opens on the latest upgrade.
    if guides := sorted((ROOT / "docs").glob("migration-*.md"), reverse=True):
        write(
            "posthog-python-migration.md",
            [GUIDES, *(nest(g.read_text(), drop_title=False) for g in guides)],
        )


if __name__ == "__main__":
    main()
