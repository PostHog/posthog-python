"""Generate this SDK's reference Markdown from source.

Griffe reads the package statically, so the SDK is never imported. Docstrings are parsed as
Google style. A `posthog-capability: <slug>` line maps a symbol to its sdk-specs capability,
and unmapped symbols land in SDK utils under their `Category:`. The shared renderer turns the
symbol list into Markdown, so every SDK's page has the same shape.

    python bin/docs_scripts/generate_markdown.py --sdk-specs <PostHog/sdk-specs checkout> [--major 8 --source-ref v1]
"""

import argparse
import json
import logging
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any

import griffe

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sdk_specs  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "https://github.com/PostHog/posthog-python/blob/v{version}/{path}#L{line}"
REFERENCES = [
    ("Client", "client.Client", "Client"),
    ("AsyncClient", "async_client.AsyncClient", "AsyncClient"),
]
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
# Where a symbol works when that isn't everywhere Python runs, by module. Extras are checked against pyproject.toml.
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
LITERAL_VALUE = re.compile(
    r"""^(None|True|False|-?\d[\d_.e]*|(["']).*\2|\[\]|\{\}|\(\))$"""
)
TAG = re.compile(r"^[ \t]*posthog-capability:[ \t]*([a-z0-9-]+)[ \t]*$\n?", re.M)
FENCE = re.compile(r"```(\w*)\n(.*?)```", re.S)
LABEL = re.compile(r"#\s*(Correct example|Counter example \(incorrect\))\.?:?\s*(.*)")
ROLE = re.compile(
    r":(?:py:)?(?:meth|func|class|attr|obj|data|exc|mod|const):`~?([^`]+)`"
)
LITERAL = re.compile(r"^([^\n]*?)::\n\n((?:(?:[ \t]+[^\n]*)?\n)+)", re.M)


def text(value) -> str:
    return str(value) if value is not None else ""


def md(value: str) -> str:
    """Converts the Sphinx markup in these docstrings to Markdown: roles, double backticks, literal blocks."""
    value = re.sub(r"``([^`]+)``", r"`\1`", ROLE.sub(r"`\1`", value or ""))

    def block(m: re.Match) -> str:
        lead = m.group(1) + ":\n\n" if m.group(1).strip() else ""
        return lead + "```python\n" + textwrap.dedent(m.group(2)).strip() + "\n```\n\n"

    return LITERAL.sub(block, value + "\n").strip()


def get(pkg, path: str):
    try:
        return pkg[path]
    except KeyError:
        return None


def available_in(path: str) -> str | None:
    module = path.removeprefix("posthog.")
    return next(
        (
            where
            for prefix, where in AVAILABLE_IN.items()
            if module == prefix or module.startswith(prefix + ".")
        ),
        None,
    )


def check_extras() -> None:
    extras = re.search(
        r"^\[project\.optional-dependencies\]\n(.*?)(?=^\[)",
        (ROOT / "pyproject.toml").read_text(),
        re.M | re.S,
    )
    named = set(re.findall(r"^(\w+) = ", extras.group(1), re.M)) if extras else set()
    missing = {
        x for w in AVAILABLE_IN.values() for x in re.findall(r"posthog\[(\w+)\]", w)
    } - named
    if missing:
        raise SystemExit(
            f"AVAILABLE_IN names extras pyproject.toml doesn't define: {sorted(missing)}"
        )


def source(obj, version: str) -> str:
    return SOURCE.format(
        version=version, path=obj.relative_package_filepath, line=obj.lineno
    )


def parse(obj) -> tuple[str | None, list]:
    raw = obj.docstring.value if obj.docstring else ""
    tag = TAG.search(raw)
    body = TAG.sub("", raw).strip()
    sections = griffe.Docstring(body, parent=obj).parse("google") if body else []
    return (tag.group(1) if tag else None), sections


def examples(value) -> list[dict]:
    out = []
    for _, code in FENCE.findall("\n".join(t for _, t in value)):
        label = why = None
        head, _, rest = code.lstrip("\n").partition("\n")
        match = LABEL.match(head.strip())
        if match:
            label, why, code = (
                "correct" if match.group(1).startswith("Correct") else "incorrect",
                match.group(2) or None,
                rest,
            )
        out.append({"code": code, "label": label, "why": why})
    return out


def typed_dict_fields(pkg, annotation: str) -> dict:
    match = re.fullmatch(r"Unpack\[(\w+)\]", annotation)
    cls = get(pkg, f"args.{match.group(1)}") if match else None
    return {n: text(a.annotation) for n, a in cls.attributes.items()} if cls else {}


def params(pkg, fn, documented: dict) -> list[dict]:
    out = []
    for p in fn.parameters:
        if p.name in ("self", "cls") or p.name.startswith("_"):
            continue
        kind = p.kind.value
        if kind == "variadic keyword":
            fields = typed_dict_fields(pkg, text(p.annotation))
            for name in [n for n in documented if n in fields]:
                out.append(
                    {
                        "name": name,
                        "type": fields[name],
                        "description": md(documented.pop(name)),
                    }
                )
            if fields:
                continue
        star = {"variadic positional": "*", "variadic keyword": "**"}.get(kind, "")
        required = p.default is None and not star
        out.append(
            {
                "name": star + p.name,
                "type": text(p.annotation),
                "default": None if star else p.default and text(p.default),
                "required": required,
                "description": md(documented.pop(p.name, "")),
            }
        )
    out += [{"name": n, "description": md(d)} for n, d in documented.items()]
    return out


def signature(fn, name: str) -> str:
    parts = []
    for p in fn.parameters:
        if p.name in ("self", "cls") or p.name.startswith("_"):
            continue
        star = {"variadic positional": "*", "variadic keyword": "**"}.get(
            p.kind.value, ""
        )
        part = star + p.name + (f": {p.annotation}" if p.annotation is not None else "")
        parts.append(
            part + (f" = {p.default}" if p.default is not None and not star else "")
        )
    ret = f" -> {fn.returns}" if fn.returns is not None and name[0].islower() else ""
    prefix = "async " if "async" in fn.labels else ""
    line = f"{prefix}{name}({', '.join(parts)}){ret}"
    return (
        line
        if len(line) <= 88
        else f"{prefix}{name}(\n" + "".join(f"    {p},\n" for p in parts) + f"){ret}"
    )


def entry(pkg, obj, qualname: str, version: str, group: str | None = None) -> dict:
    slug, sections = parse(obj)
    e: dict[str, Any] = {
        "qualname": qualname,
        "capability": slug,
        "group": group,
        "description": "",
        "examples": [],
        "source": source(obj, version),
        "availableIn": available_in(obj.module.path),
    }
    texts, notes, documented = [], [], {}
    for s in sections:
        kind = s.kind.value
        if kind == "text":
            texts.append(s.value)
        elif kind in ("parameters", "other parameters"):
            documented.update({p.name: p.description for p in s.value})
        elif kind == "returns":
            e["returns"] = {
                "type": text(s.value[0].annotation)
                or text(getattr(obj, "returns", "")),
                "description": md(" ".join(r.description for r in s.value)),
            }
        elif kind == "raises":
            e["raises"] = [
                {"type": text(r.annotation), "description": md(r.description)}
                for r in s.value
            ]
        elif kind == "examples":
            e["examples"] += examples(s.value)
        elif kind == "deprecated":
            e["deprecated"] = s.value.description
        elif kind == "admonition" and s.title in ("Example", "Examples"):
            body = s.value.description
            e["examples"] += (
                examples([(None, body)])
                if "```" in body
                else [{"code": body, "label": None, "why": None}]
            )
        elif kind == "admonition" and s.title == "Category":
            e["group"] = group or s.value.description.strip()
        elif kind == "admonition":
            notes.append(f"**{s.title}.** {md(s.value.description)}")
    e["description"], e["notes"] = md("\n\n".join(texts)), "\n\n".join(notes)
    if obj.is_function:
        name = qualname.removesuffix("(...)")
        e["signature"] = signature(obj, name if obj.name == "__init__" else obj.name)
        e["params"] = params(pkg, obj, documented)
        if not e.get("returns") and obj.returns is not None and obj.name != "__init__":
            e["returns"] = {"type": text(obj.returns)}
    elif obj.is_class and "__init__" in obj.members:
        init = obj.members["__init__"]
        e["signature"] = signature(init, obj.name)
        e["params"] = params(
            pkg,
            init,
            documented
            | {
                p.name: p.description
                for s in parse(init)[1]
                if s.kind.value == "parameters"
                for p in s.value
            },
        )
    elif obj.is_attribute:
        e["signature"] = (
            f"{qualname}: {obj.annotation}" if obj.annotation is not None else None
        )
    return e


def members(cls) -> list:
    return [
        m
        for n, m in cls.members.items()
        if not m.is_alias
        and (not n.startswith("_") or n == "__init__")
        and (m.is_function or "property" in m.labels)
    ]


def documented_fields(cls) -> dict:
    """Field descriptions from the class's `Attributes:`, `Args:` and `Values:` sections, and from `__init__`'s `Args:`."""
    sections = parse(cls)[1] + (
        parse(cls.members["__init__"])[1] if "__init__" in cls.members else []
    )
    found = {
        item.name: item.description
        for s in sections
        if s.kind.value in ("attributes", "parameters")
        for item in s.value
    }
    for s in sections:
        if s.kind.value == "admonition" and s.title == "Values":
            found |= {
                n: " ".join(d.split())
                for n, d in re.findall(
                    r"^(\w+): (.*?)(?=^\w+:|\Z)", s.value.description, re.M | re.S
                )
            }
    return found


def is_data(cls) -> bool:
    """Dataclasses, TypedDicts, enums and errors carry fields readers use. Service objects carry only state."""
    bases = " ".join(text(b) for b in cls.bases)
    return any("dataclass" in text(d.value) for d in cls.decorators) or any(
        k in bases for k in ("TypedDict", "Enum", "Exception", "Error")
    )


def type_info(pkg, cls, version: str) -> dict:
    _, sections = parse(cls)
    documented = documented_fields(cls)
    # Only a literal is a default. A field set from an __init__ argument shows that argument's name instead.
    enum = any("Enum" in text(b) for b in cls.bases)

    def value(a):
        return text(a.value) if LITERAL_VALUE.match(text(a.value)) else None

    fields = [
        {
            "name": n,
            "type": None if enum else text(a.annotation),
            "default": None if enum else value(a),
            "value": value(a) if enum else None,
            "description": md(
                a.docstring.value if a.docstring else documented.get(n, "")
            ),
        }
        for n, a in cls.attributes.items()
        if is_data(cls) and not n.startswith("_") and "property" not in a.labels
    ]
    methods = [
        entry(pkg, m, f"{cls.name}.{m.name}", version)
        for m in members(cls)
        if m.name != "__init__"
    ]
    description = md("\n\n".join(s.value for s in sections if s.kind.value == "text"))
    return {
        "name": cls.name,
        "description": description,
        "fields": fields,
        "methods": methods,
        "source": source(cls, version),
    }


def configs(pkg) -> list[dict]:
    out = []
    for path, where in CONFIGS:
        cls = get(pkg, path)
        doc = cls.docstring.value if cls and cls.docstring else ""
        for name, desc in re.findall(
            r"^\s*- `(POSTHOG_[A-Z_]+)`: (.*?)(?=^\s*- `|^\s*$)", doc, re.M | re.S
        ):
            out.append(
                {
                    "name": name,
                    "where": where,
                    "description": md(" ".join(desc.split())),
                }
            )
    return out


def page(pkg, version: str, major: int, status: str) -> dict:
    refs: list[dict[str, Any]] = []
    for title, path, prefix in REFERENCES:
        cls = pkg[path]
        refs.append(
            {
                "title": title,
                "entries": [
                    entry(
                        pkg,
                        m,
                        f"{prefix}(...)"
                        if m.name == "__init__"
                        else f"{prefix}.{m.name}",
                        version,
                    )
                    for m in members(cls)
                ],
            }
        )
    module = [
        entry(pkg, m, f"posthog.{n}", version)
        for n, m in pkg.members.items()
        if not m.is_alias and m.is_function and not n.startswith("_")
    ]
    refs.append({"title": "Module functions", "entries": module})
    for group, paths in UTILS.items():
        for path in paths:
            if (obj := get(pkg, path)) is not None:
                refs[0]["entries"].append(
                    entry(pkg, obj, f"posthog.{path}", version, group)
                )
                if obj.is_class:
                    refs[0]["entries"] += [
                        entry(pkg, m, f"{obj.name}.{m.name}", version, group)
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
            refs[0]["entries"].append(
                {
                    "qualname": f"posthog.ai.{name}",
                    "group": "AI wrapper clients",
                    "availableIn": available_in(f"ai.{name}"),
                    "description": " ".join(
                        filter(None, [summary, f"Exports {names}."])
                    ),
                }
            )
    # Newest guide first, so the migration page opens on the latest upgrade.
    migrations = sorted((ROOT / "docs").glob("migration-*.md"), reverse=True)
    return {
        "sdk": "python",
        "title": "PostHog Python SDK",
        "version": version,
        "major": major,
        "status": status,
        "language": "python",
        "gettingStarted": (ROOT / "docs/getting-started.md").read_text(),
        "references": refs,
        "configs": configs(pkg),
        "types": [type_info(pkg, c, version) for p in TYPES if (c := get(pkg, p))],
        "migrations": [m.read_text() for m in migrations],
    }


def main() -> None:
    args = argparse.ArgumentParser()
    args.add_argument(
        "--sdk-specs",
        required=True,
        help="Checkout of PostHog/sdk-specs to take capabilities from",
    )
    args.add_argument(
        "--renderer", default=str(Path(__file__).resolve().parent / "render.mjs")
    )
    args.add_argument(
        "--major",
        type=int,
        help="Major this branch targets, defaults to the declared version's major",
    )
    args.add_argument(
        "--source-ref",
        help="Git ref for source links, defaults to the release tag v<version>",
    )
    opts = args.parse_args()
    logging.getLogger("griffe").setLevel(logging.ERROR)
    check_extras()
    pkg = griffe.load(
        "posthog",
        search_paths=[str(ROOT)],
        docstring_parser="google",
        resolve_aliases=False,
    )
    found = re.search(r'VERSION = "([^"]+)"', (ROOT / "posthog/version.py").read_text())
    if found is None:
        raise SystemExit("posthog/version.py has no VERSION")
    declared = found.group(1)
    major = opts.major or int(declared.split(".")[0])
    # A branch that targets a newer major than it has released documents an experimental reference.
    status = "current" if int(declared.split(".")[0]) >= major else "experimental"
    version = declared if status == "current" else "unreleased"
    global SOURCE
    SOURCE = SOURCE.replace("v{version}", opts.source_ref or "v{version}")
    data = page(pkg, version, major, status)
    outputs = {
        f"posthog-python-{data['major']}.md": [],
        "posthog-python-migration.md": ["--migration"],
    }
    with (
        tempfile.NamedTemporaryFile("w", suffix=".json") as f,
        tempfile.NamedTemporaryFile("w", suffix=".json") as caps,
    ):
        json.dump(data, f)
        json.dump(sdk_specs.capabilities(opts.sdk_specs), caps)
        f.flush()
        caps.flush()
        for name, flag in outputs.items():
            md = subprocess.run(
                ["node", opts.renderer, f.name, caps.name, *flag],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            if md:
                (ROOT / "references" / name).write_text(md)
                print(f"wrote references/{name}")


if __name__ == "__main__":
    main()
