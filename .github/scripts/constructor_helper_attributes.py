"""Recover public instance attributes initialized by private constructor helpers."""

from __future__ import annotations

import ast
from typing import Iterator

from griffe import Class, Extension, Function, Visitor, ast_children


_DEFERRED_SCOPES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Lambda,
    ast.GeneratorExp,
)


def _eager_nodes(node: ast.AST) -> Iterator[ast.AST]:
    if isinstance(node, _DEFERRED_SCOPES):
        return
    yield node
    for child in ast_children(node):
        yield from _eager_nodes(child)


def _public_instance_target(node: ast.AST) -> ast.Attribute | None:
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, ast.AnnAssign):
        targets = [node.target]
    else:
        return None
    if (
        len(targets) == 1
        and isinstance(target := targets[0], ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "self"
        and not target.attr.startswith("_")
    ):
        return target
    return None


class ConstructorHelperAttributes(Extension):
    """Add missing attributes from direct, same-class private helper calls.

    Griffe ignores method-body assignments outside ``__init__``. Replay only
    simple public instance assignments from reachable synchronous, undecorated helpers,
    keeping existing constructor and class records unchanged.
    """

    def on_class_members(self, *, node, cls: Class, agent, **kwargs) -> None:
        if not isinstance(node, ast.ClassDef) or not isinstance(agent, Visitor):
            return
        methods = {
            method.name: method
            for method in node.body
            if isinstance(method, ast.FunctionDef)
            and not method.decorator_list
            and method.args.args
            and method.args.args[0].arg == "self"
        }
        constructor = cls.members.get("__init__")
        if not isinstance(constructor, Function) or "__init__" not in methods:
            return
        existing_names = set(cls.members)
        previous = agent.current
        try:
            # Griffe's constructor visitor also stores Function despite this annotation.
            agent.current = constructor  # type: ignore[assignment]
            self._follow(
                methods["__init__"], methods, agent, existing_names, set(), False
            )
        finally:
            agent.current = previous

    def _follow(self, method, methods, agent, existing_names, active, in_helper):
        if method.name in active:
            return
        active.add(method.name)
        try:
            # ast_children supplies parent links required by Griffe's assignment visitor.
            for statement in ast_children(method):
                if statement not in method.body:
                    continue
                for node in _eager_nodes(statement):
                    target = _public_instance_target(node) if in_helper else None
                    if target is not None and target.attr not in existing_names:
                        agent.visit(node)
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "self"
                        and node.func.attr.startswith("_")
                        and not node.func.attr.startswith("__")
                        and node.func.attr in methods
                    ):
                        self._follow(
                            methods[node.func.attr],
                            methods,
                            agent,
                            existing_names,
                            active,
                            True,
                        )
        finally:
            active.remove(method.name)
