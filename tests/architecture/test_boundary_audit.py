"""Hard configuration-boundary assertions, intentionally not baselines."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from tests.architecture.source_inspect import SRC, production_sources


CTAM = "EdgeWARN/ctam/"
ENV_READERS = {"common/config/loader.py", "common/config/overlay.py", "config/loader.js"}
URL_LITERAL_EXCEPTIONS = {
    ("api/middleware/logging.js", "http://edgewarn.invalid"),
    # The private loopback CTAM API is bound to 127.0.0.1 on an OS-assigned
    # ephemeral port and is never reachable off-host; docs/ctam pins the URL
    # shape. It is deliberately not a public, deployable endpoint.
    ("EdgeWARN/ctam/api/server.py", "http://127.0.0.1:"),
    # Configurable MRMS phase 2 deliberately moves protected source identity
    # into code. Operational timeouts/retention remain catalog-owned.
    ("common/ingest/mrms/source.py", "https://mrms.ncep.noaa.gov/data/2D"),
    ("common/ingest/mrms/source.py", "https://mrms.ncep.noaa.gov/data/ProbSevere"),
}
# These are library-call defaults rather than deployable policy. Keep them
# enumerated so a new operational default cannot quietly join this boundary.
_CATALOG_NAME = re.compile(r"(?:product|event|pressure|catalog|channel|modifier|vcp)", re.I)
_OPERATIONAL_NAME = re.compile(r"(?:timeout|poll|interval|retention|cache|worker|retry|backoff|cleanup|age|limit|ttl)", re.I)


def _relative(path: Path, root: Path = SRC) -> str:
    return path.relative_to(root).as_posix()


def _python_docstrings(tree: ast.AST) -> set[ast.Constant]:
    values = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                values.add(first.value)
    return values


def _url_literals(root: Path = SRC) -> list[tuple[str, str]]:
    found = []
    for path in production_sources(root):
        relative = _relative(path, root)
        source = path.read_text(encoding="utf-8", errors="ignore")
        if path.suffix == ".py":
            tree = ast.parse(source)
            docstrings = _python_docstrings(tree)
            values = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str) and node not in docstrings]
        else:
            values = re.findall(r"[\"'](https?://[^\"']+)[\"']", source)
        found.extend((relative, value) for value in values if value.startswith(("http://", "https://")) and (relative, value) not in URL_LITERAL_EXCEPTIONS)
    return found


def _env_reads(root: Path = SRC) -> list[str]:
    found = []
    for path in production_sources(root):
        relative = _relative(path, root)
        source = path.read_text(encoding="utf-8", errors="ignore")
        if path.suffix == ".py":
            tree = ast.parse(source)
            for node in ast.walk(tree):
                target = node.value if isinstance(node, ast.Subscript) else node.func if isinstance(node, ast.Call) else None
                if isinstance(target, ast.Attribute) and target.attr in {"environ", "get"} and isinstance(target.value, ast.Attribute) and target.value.attr == "environ":
                    if isinstance(node, ast.Subscript) or target.attr == "get":
                        found.append(relative)
        elif re.search(r"\bprocess\.env\b", source):
            found.append(relative)
    return sorted(set(found) - ENV_READERS - {"api/config/index.js"})


def _config_path_accesses(root: Path = SRC) -> list[str]:
    found = []
    for path in production_sources(root):
        relative = _relative(path, root)
        if relative in ENV_READERS:
            continue
        source = path.read_text(encoding="utf-8", errors="ignore")
        if "__file__" in source and re.search(r"[\"']config[\"']", source):
            found.append(relative)
    return found


def _literal_collection(node: ast.AST) -> bool:
    return isinstance(node, (ast.Dict, ast.List, ast.Set, ast.Tuple))


def _numeric_literal(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool)


def _assigned_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return [target.id for target in targets if isinstance(target, ast.Name)]


def _function_defaults(tree: ast.AST, scope: str = ""):
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            yield from _function_defaults(node, f"{scope}{node.name}.")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args.posonlyargs + node.args.args
            defaults = [None] * (len(args) - len(node.args.defaults)) + list(node.args.defaults)
            for arg, default in zip(args, defaults):
                if _numeric_literal(default):
                    yield f"{scope}{node.name}", arg.arg
            for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
                if _numeric_literal(default):
                    yield f"{scope}{node.name}", arg.arg
            yield from _function_defaults(node, f"{scope}{node.name}.")
        else:
            yield from _function_defaults(node, scope)


def test_production_url_literals_stay_with_explicit_owners():
    assert _url_literals() == []


def test_environment_reads_stay_in_configuration_infrastructure():
    assert _env_reads() == []


def test_no_source_relative_config_access_bypasses_loaders():
    assert _config_path_accesses() == []


def _runtime_section_calls():
    """Yield (path, line, name) for every ``util.runtime.config.section`` call.

    Several modules define their own ``section`` helper for other catalogs, so
    only names imported from ``util.runtime.config`` (or ``.config`` inside
    ``util/runtime``) are audited.
    """
    for path in production_sources():
        if path.suffix != ".py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        relative = path.relative_to(SRC).as_posix()
        local = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            runtime_config = node.module == "util.runtime.config" or (
                node.level == 1 and node.module == "config"
                and relative.startswith("util/runtime/"))
            if runtime_config:
                local.update(alias.asname or alias.name for alias in node.names
                             if alias.name == "section")
        if not local:
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in local and node.args):
                argument = node.args[0]
                name = argument.value if isinstance(argument, ast.Constant) else None
                yield relative, node.lineno, name
