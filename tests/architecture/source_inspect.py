"""Read declared defaults straight out of the source tree.

The duplicate-default audit has to name a literal at a specific location, and
most of the modules holding those literals cannot be imported without the full
scientific stack. Parsing with ``ast`` keeps the audit runnable anywhere and
makes the assertion about the declaration rather than a runtime value that some
caller may already have overridden.
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"


def production_sources(root: Path = SRC):
    """Yield Python and JavaScript production sources, including NUL-containing JS."""
    for path in root.rglob("*"):
        if path.suffix not in {".py", ".js"} or {"__pycache__", "node_modules"} & set(path.parts):
            continue
        yield path

class _Missing:
    """Marks a default that is an expression rather than a literal.

    A stable ``repr`` keeps it usable inside a committed snapshot.
    """

    def __repr__(self) -> str:
        return "<not a literal>"


_MISSING = _Missing()


@functools.lru_cache(maxsize=None)
def _parse(relative_path: str) -> ast.Module:
    return ast.parse((SRC / relative_path).read_text(encoding="utf-8"))


def _literal(node: ast.AST):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return _MISSING


def _find_function(module: ast.Module, qualname: str) -> ast.FunctionDef:
    parts = qualname.split(".")
    scope: list[ast.AST] = list(module.body)
    target = None
    for index, part in enumerate(parts):
        target = None
        for node in scope:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == part:
                target = node
                break
        assert target is not None, f"{qualname!r}: could not resolve {part!r}"
        if index < len(parts) - 1:
            scope = list(target.body)
    assert isinstance(target, (ast.FunctionDef, ast.AsyncFunctionDef)), f"{qualname!r} is not a function"
    return target


def param_default(relative_path: str, qualname: str, param: str):
    """Return the declared default for a positional or keyword-only parameter."""
    function = _find_function(_parse(relative_path), qualname)
    args = function.args

    positional = args.posonlyargs + args.args
    padding = len(positional) - len(args.defaults)
    for index, arg in enumerate(positional):
        if arg.arg == param:
            assert index >= padding, f"{qualname}.{param} has no default"
            return _literal(args.defaults[index - padding])

    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        if arg.arg == param:
            assert default is not None, f"{qualname}.{param} has no default"
            return _literal(default)

    raise AssertionError(f"{qualname} has no parameter {param!r}")


def has_param_default(relative_path: str, qualname: str, param: str) -> bool:
    try:
        param_default(relative_path, qualname, param)
    except AssertionError:
        return False
    return True


def module_constant(relative_path: str, name: str):
    """Return a module-level constant's literal value."""
    for node in _parse(relative_path).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return _literal(node.value)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name and node.value is not None:
                return _literal(node.value)
    raise AssertionError(f"{relative_path} has no module constant {name!r}")


def argparse_defaults(relative_path: str) -> dict[str, object]:
    """Map every ``add_argument`` flag in a file to its declared default.

    A flag with no ``default=`` is reported as ``None`` for value options and
    ``False`` for ``store_true``, matching argparse's own behavior, so the
    precedence work can see which flags cannot express "unset".
    """
    defaults: dict[str, object] = {}
    for node in ast.walk(_parse(relative_path)):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not (isinstance(function, ast.Attribute) and function.attr == "add_argument"):
            continue

        flags = [value for value in (_literal(a) for a in node.args) if isinstance(value, str)]
        if not flags:
            continue

        keywords = {k.arg: k.value for k in node.keywords if k.arg}
        action = _literal(keywords["action"]) if "action" in keywords else None
        if "default" in keywords:
            default = _literal(keywords["default"])
        elif action == "store_true":
            default = False
        else:
            default = None

        primary = next((flag for flag in flags if flag.startswith("--")), flags[0])
        defaults[primary] = {"default": default, "action": action}
    return defaults
