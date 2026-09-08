"""Hard configuration-boundary assertions, intentionally not baselines."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from tests.architecture.source_inspect import SRC, production_sources


CTAM = "EdgeWARN/ctam/"
ENV_READERS = {"common/config/loader.py", "common/config/overlay.py", "config/loader.js"}
URL_SENTINELS = {
    ("api/middleware/logging.js", "http://edgewarn.invalid"),
    # The private loopback CTAM API is bound to 127.0.0.1 on an OS-assigned
    # ephemeral port and is never reachable off-host; docs/ctam pins the URL
    # shape. It is deliberately not a public, deployable endpoint.
    ("EdgeWARN/ctam/api/server.py", "http://127.0.0.1:"),
}
CATALOG_REGISTRIES = {
    "EWMRS/rap/config.py": "the Phase 5 code-owned Uint16 display registry",
    CTAM: "explicitly out of scope for this configuration plan",
}
# These are library-call defaults rather than deployable policy. Keep them
# enumerated so a new operational default cannot quietly join this boundary.
OPERATIONAL_LITERAL_EXCEPTIONS = {
    ("EWMRS/pipeline.py", "<module>", "_LAST_GOES_GUI_CLEANUP_S"): "cleanup timestamp state, not a cleanup policy",
    ("EdgeWARN/alerts/manager.py", "AlertManager.cleanup_expired", "max_age_minutes"): "CTAM alert cleanup is deferred",
    ("EdgeWARN/process/detect/track.py", "<module>", "_KALMAN_INITIALIZED_IDS_SAMPLE_LIMIT"): "bounded diagnostic output",
    ("EdgeWARN/process/detect/track.py", "StormCellTracker._format_id_sample", "limit"): "bounded diagnostic output",
    ("common/ingest/nexrad/s3_async.py", "AsyncNexradChunkStore.async_list_recent_volume_ids", "limit"): "one-result helper default",
    ("common/ingest/nexrad/s3_async.py", "async_list_recent_volume_ids", "limit"): "one-result helper default",
    ("common/ingest/nexrad/s3_chunks.py", "NexradChunkStore.list_recent_volume_ids", "limit"): "one-result helper default",
    ("common/ingest/nexrad/s3_chunks.py", "list_recent_volume_ids", "limit"): "one-result helper default",
    ("util/runtime/timing.py", "sleep_for", "interval"): "generic interruptible-sleep helper",
    ("api/services/serviceRegistry.js", "<module>", "CACHE_TTL_MS"): "heartbeat scanner cache granularity from the decomposition plan, not deployable policy",
    ("api/services/serviceRegistry.js", "<parameter>", "CACHE_TTL_MS"): "same scanner-cache constant seen by the JS literal scanner",
    ("run_edgewarn.py", "<module>", "HEARTBEAT_MIN_INTERVAL_SECONDS"): "heartbeat write throttle inside the primary's own tick hook, not deployable policy",
    ("run_edgewarn.py", "<parameter>", "HEARTBEAT_MIN_INTERVAL_SECONDS"): "same heartbeat throttle constant seen by the literal scanner",
}
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
        found.extend((relative, value) for value in values if value.startswith(("http://", "https://")) and (relative, value) not in URL_SENTINELS)
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


def _is_catalog_registry(relative: str) -> bool:
    return any(relative == path or relative.startswith(path) for path in CATALOG_REGISTRIES)


def _literal_collection(node: ast.AST) -> bool:
    return isinstance(node, (ast.Dict, ast.List, ast.Set, ast.Tuple))


def _numeric_literal(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool)


def _assigned_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return [target.id for target in targets if isinstance(target, ast.Name)]


def _source_catalogs(root: Path = SRC) -> list[str]:
    """Find module-level literal catalog declarations outside typed registries.

    Runtime collections such as ``rendered_layers`` are deliberately excluded:
    they accumulate the result of work and are not product authorities.
    """
    found = []
    for path in production_sources(root):
        relative = _relative(path, root)
        if _is_catalog_registry(relative):
            continue
        source = path.read_text(encoding="utf-8", errors="ignore")
        if path.suffix == ".py":
            for node in ast.parse(source).body:
                if not isinstance(node, (ast.Assign, ast.AnnAssign)) or not _literal_collection(node.value):
                    continue
                found.extend(f"{relative}:{name}" for name in _assigned_names(node) if _CATALOG_NAME.search(name))
        else:
            pattern = r"(?:const|let|var)\s+(\w*(?:product|event|pressure|catalog|channel|modifier|vcp)\w*)\s*=\s*(?:\[|\{|new\s+Set\s*\(\s*\[)"
            found.extend(f"{relative}:{name}" for name in re.findall(pattern, source, re.I))
    return sorted(found)


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


def _operational_numeric_literals(root: Path = SRC) -> list[tuple[str, str, str]]:
    """Find numeric policy defaults that would compete with YAML settings."""
    found = []
    for path in production_sources(root):
        relative = _relative(path, root)
        if relative.startswith(CTAM):
            continue
        source = path.read_text(encoding="utf-8", errors="ignore")
        if path.suffix == ".py":
            tree = ast.parse(source)
            for function, parameter in _function_defaults(tree):
                entry = (relative, function, parameter)
                if _OPERATIONAL_NAME.search(parameter) and entry not in OPERATIONAL_LITERAL_EXCEPTIONS:
                    found.append(entry)
            for node in tree.body:
                if isinstance(node, (ast.Assign, ast.AnnAssign)) and _numeric_literal(node.value):
                    for name in _assigned_names(node):
                        entry = (relative, "<module>", name)
                        if _OPERATIONAL_NAME.search(name) and entry not in OPERATIONAL_LITERAL_EXCEPTIONS:
                            found.append(entry)
        else:
            pattern = r"(?:const|let|var)\s+(\w*(?:timeout|poll|interval|retention|cache|worker|retry|backoff|cleanup|age|limit|ttl)\w*)\s*=\s*\d+(?:\.\d+)?\b"
            for name in re.findall(pattern, source, re.I):
                entry = (relative, "<module>", name)
                if entry not in OPERATIONAL_LITERAL_EXCEPTIONS:
                    found.append(entry)
            parameter_pattern = r"(\w*(?:timeout|poll|interval|retention|cache|worker|retry|backoff|cleanup|age|limit|ttl)\w*)\s*=\s*\d+(?:\.\d+)?\b"
            for name in re.findall(parameter_pattern, source, re.I):
                entry = (relative, "<parameter>", name)
                if entry not in OPERATIONAL_LITERAL_EXCEPTIONS:
                    found.append(entry)
    return sorted(found)


def test_production_url_literals_are_not_endpoints():
    assert _url_literals() == []


def test_environment_reads_stay_in_configuration_infrastructure():
    assert _env_reads() == []


def test_product_catalogs_are_limited_to_typed_or_out_of_scope_registries():
    assert _source_catalogs() == []


def test_operational_numeric_defaults_are_explicitly_approved():
    assert _operational_numeric_literals() == []


def test_no_source_relative_config_access_bypasses_loaders():
    assert _config_path_accesses() == []


def test_audit_scanners_reject_negative_controls(tmp_path):
    source = tmp_path / "x.py"
    javascript = tmp_path / "x.js"
    source.write_text('URL = "https://new.example"\nimport os\nvalue = os.environ.get("NEW")\npath = __file__ / "config"\nPRODUCTS = ["new"]\nPOLL_INTERVAL_SECONDS = 30\ndef wait(poll_interval=30): pass\n', encoding="utf-8")
    javascript.write_text("const products = ['new'];\nfunction wait(pollInterval = 30) {}\n", encoding="utf-8")
    assert _url_literals(tmp_path) == [("x.py", "https://new.example")]
    assert _env_reads(tmp_path) == ["x.py"]
    assert _config_path_accesses(tmp_path) == ["x.py"]
    assert _source_catalogs(tmp_path) == ["x.js:products", "x.py:PRODUCTS"]
    assert _operational_numeric_literals(tmp_path) == [("x.js", "<parameter>", "pollInterval"), ("x.py", "<module>", "POLL_INTERVAL_SECONDS"), ("x.py", "wait", "poll_interval")]
