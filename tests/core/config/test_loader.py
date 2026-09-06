"""Unit tests for the hand-rolled schema walker in ``common.config.loader``.

There is no upstream test suite backing this validator (unlike a library),
so each keyword it implements gets its own regression test here.
"""

from __future__ import annotations

import json
import os

import pytest
import yaml

from common.config import loader as config_loader


def _config_dir(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "runtime.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    (tmp_path / "schema").mkdir(exist_ok=True)
    return tmp_path


def _write(config_dir, name, document, schema):
    (config_dir / f"{name}.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    (config_dir / "schema" / f"{name}.schema.json").write_text(json.dumps(schema), encoding="utf-8")


@pytest.fixture
def config_dir(tmp_path):
    yield _config_dir(tmp_path)
    config_loader.reset_cache()


def test_valid_document_passes(config_dir):
    schema = {
        "type": "object",
        "required": ["a"],
        "additionalProperties": False,
        "properties": {"a": {"type": "integer"}},
    }
    _write(config_dir, "sample", {"a": 1}, schema)

    loaded = config_loader.load_config("sample", config_dir=config_dir)

    assert loaded["a"] == 1


def test_validate_document_uses_the_same_schema_and_error_paths(config_dir, monkeypatch):
    schema = {
        "type": "object",
        "required": ["a"],
        "additionalProperties": False,
        "properties": {"a": {"type": "integer", "minimum": 1}},
    }
    _write(config_dir, "sample", {"a": 1}, schema)
    monkeypatch.setattr(config_loader, "CONFIG_NAMES", (*config_loader.CONFIG_NAMES, "sample"))

    config_loader.validate_document("sample", {"a": 2}, config_dir=config_dir)
    with pytest.raises(config_loader.ConfigError) as excinfo:
        config_loader.validate_document("sample", {"a": 0}, config_dir=config_dir)

    assert excinfo.value.filename == "sample.yaml"
    assert excinfo.value.dotted_path == "a"
    assert excinfo.value.message == "must be >= 1"


def test_missing_required_property_raises(config_dir):
    schema = {
        "type": "object",
        "required": ["a"],
        "additionalProperties": False,
        "properties": {"a": {"type": "integer"}},
    }
    _write(config_dir, "sample", {}, schema)

    with pytest.raises(config_loader.ConfigError) as excinfo:
        config_loader.load_config("sample", config_dir=config_dir)

    assert excinfo.value.dotted_path == "a"
    assert "required" in excinfo.value.message


def test_unexpected_property_rejected(config_dir):
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"a": {"type": "integer"}},
    }
    _write(config_dir, "sample", {"a": 1, "typo": 2}, schema)

    with pytest.raises(config_loader.ConfigError) as excinfo:
        config_loader.load_config("sample", config_dir=config_dir)

    assert excinfo.value.dotted_path == "typo"


def test_type_mismatch_raises(config_dir):
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"a": {"type": "integer"}},
    }
    _write(config_dir, "sample", {"a": "not-an-int"}, schema)

    with pytest.raises(config_loader.ConfigError) as excinfo:
        config_loader.load_config("sample", config_dir=config_dir)

    assert excinfo.value.dotted_path == "a"
    assert "not of type" in excinfo.value.message


def test_boolean_is_not_accepted_as_integer(config_dir):
    schema = {"type": "object", "additionalProperties": False, "properties": {"a": {"type": "integer"}}}
    _write(config_dir, "sample", {"a": True}, schema)

    with pytest.raises(config_loader.ConfigError):
        config_loader.load_config("sample", config_dir=config_dir)


def test_additional_properties_as_schema_validates_map_values(config_dir):
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "env_overrides": {"type": "object", "additionalProperties": {"type": "string"}},
        },
    }
    _write(config_dir, "sample", {"env_overrides": {"A": "1", "B": 2}}, schema)

    with pytest.raises(config_loader.ConfigError) as excinfo:
        config_loader.load_config("sample", config_dir=config_dir)

    assert excinfo.value.dotted_path == "env_overrides.B"


def test_array_min_max_items_and_unique_items(config_dir):
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "bounds": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 2,
                "maxItems": 2,
                "uniqueItems": True,
            }
        },
    }
    _write(config_dir, "sample", {"bounds": [1, 1]}, schema)

    with pytest.raises(config_loader.ConfigError) as excinfo:
        config_loader.load_config("sample", config_dir=config_dir)

    assert "unique" in excinfo.value.message


def test_array_length_bounds_enforced(config_dir):
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"bounds": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4}},
    }
    _write(config_dir, "sample", {"bounds": [1, 2]}, schema)

    with pytest.raises(config_loader.ConfigError) as excinfo:
        config_loader.load_config("sample", config_dir=config_dir)

    assert "at least 4" in excinfo.value.message


def test_numeric_bounds_inclusive_and_exclusive(config_dir):
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "count": {"type": "integer", "minimum": 0, "maximum": 10},
            "ratio": {"type": "number", "exclusiveMinimum": 0},
        },
    }
    _write(config_dir, "sample", {"count": 11, "ratio": 0}, schema)

    with pytest.raises(config_loader.ConfigError) as excinfo:
        config_loader.load_config("sample", config_dir=config_dir)

    assert excinfo.value.dotted_path in ("count", "ratio")


def test_pattern_mismatch_raises(config_dir):
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"color": {"type": "string", "pattern": "^#[0-9A-Fa-f]{6}$"}},
    }
    _write(config_dir, "sample", {"color": "not-a-hex-color"}, schema)

    with pytest.raises(config_loader.ConfigError) as excinfo:
        config_loader.load_config("sample", config_dir=config_dir)

    assert "pattern" in excinfo.value.message


def test_const_and_enum_enforced(config_dir):
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema_version": {"const": 1},
            "level": {"enum": ["low", "medium", "high"]},
        },
    }
    _write(config_dir, "sample", {"schema_version": 2, "level": "extreme"}, schema)

    with pytest.raises(config_loader.ConfigError):
        config_loader.load_config("sample", config_dir=config_dir)


def test_unsupported_schema_keyword_is_a_startup_error(config_dir):
    schema = {
        "type": "object",
        "properties": {"a": {"oneOf": [{"type": "integer"}, {"type": "string"}]}},
    }
    _write(config_dir, "sample", {"a": 1}, schema)

    with pytest.raises(config_loader.ConfigError) as excinfo:
        config_loader.load_config("sample", config_dir=config_dir)

    assert "unsupported schema keyword" in excinfo.value.message
    assert excinfo.value.filename.endswith("sample.schema.json")


def test_export_config_root_publishes_the_root_a_child_would_resolve(config_dir, monkeypatch):
    """`--config-dir` only reaches a child process through the environment.

    A spawned child gets no argv and, for a NEXRAD parse worker, no config in its
    submit payload, so it calls `config_root()` with no argument. Exporting is
    what makes that no-argument call agree with the parent instead of walking up
    to the repo default.
    """
    monkeypatch.delenv("EDGEWARN_CONFIG_DIR", raising=False)
    assert config_loader.config_root() != config_dir, "fixture must differ from the repo default"

    returned = config_loader.export_config_root(config_dir)

    assert returned == config_dir
    assert config_loader.config_root() == config_dir


def test_a_changed_env_root_is_honoured_without_clearing_the_cache(tmp_path, monkeypatch):
    """Root resolution is memoized, and the memo must be keyed by its input.

    Resolving costs a `resolve()` plus a stat, which every accessor pays on
    every read, so the result is cached. Caching it under no key -- an
    `lru_cache` over `config_root()`, whose only argument is `None` on the env
    path -- would pin whichever root resolved first for the life of the process
    and put a later `EDGEWARN_CONFIG_DIR` out of reach. That is the same freeze
    the per-call accessor convention exists to avoid, one layer lower down.
    """
    first = _config_dir(tmp_path / "first")
    second = _config_dir(tmp_path / "second")

    monkeypatch.setenv("EDGEWARN_CONFIG_DIR", str(first))
    assert config_loader.config_root() == first

    monkeypatch.setenv("EDGEWARN_CONFIG_DIR", str(second))
    assert config_loader.config_root() == second, "the memo outranked the environment"

    monkeypatch.delenv("EDGEWARN_CONFIG_DIR")
    assert config_loader.config_root() not in (first, second)


def test_a_relative_root_is_resolved_against_the_current_directory_each_time(tmp_path, monkeypatch):
    """A relative root is not a function of its key alone, so it must not be memoized.

    `Path("config").resolve()` answers differently from a different working
    directory. Memoizing it by name would hand back the previous directory's
    config after a chdir -- silently, since both paths exist.
    """
    _config_dir(tmp_path / "one" / "config")
    _config_dir(tmp_path / "two" / "config")
    monkeypatch.delenv("EDGEWARN_CONFIG_DIR", raising=False)

    monkeypatch.chdir(tmp_path / "one")
    assert config_loader.config_root("config") == (tmp_path / "one" / "config").resolve()

    monkeypatch.chdir(tmp_path / "two")
    assert config_loader.config_root("config") == (tmp_path / "two" / "config").resolve()


def test_export_config_root_rejects_a_directory_without_runtime_yaml(tmp_path, monkeypatch):
    """Publishing an invalid root would hand every child a broken override."""
    monkeypatch.delenv("EDGEWARN_CONFIG_DIR", raising=False)

    with pytest.raises(config_loader.ConfigError):
        config_loader.export_config_root(tmp_path)

    assert "EDGEWARN_CONFIG_DIR" not in os.environ


def test_loaded_config_is_recursively_frozen(config_dir):
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "nested": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"items": {"type": "array", "items": {"type": "integer"}}},
            }
        },
    }
    _write(config_dir, "sample", {"nested": {"items": [1, 2, 3]}}, schema)

    loaded = config_loader.load_config("sample", config_dir=config_dir)

    with pytest.raises(TypeError):
        loaded["nested"]["items"] = ()
    assert isinstance(loaded["nested"]["items"], tuple)


def test_provenance_reports_the_path_and_version_of_a_loaded_catalog(config_dir):
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"schema_version": {"type": "integer"}, "a": {"type": "integer"}},
    }
    _write(config_dir, "sample", {"schema_version": 4, "a": 1}, schema)

    provenance = config_loader.get_provenance("sample", config_dir=config_dir)

    assert provenance == {
        "path": str(config_dir / "sample.yaml"),
        "schema_version": 4,
    }


# --- Path token expansion -------------------------------------------------
#
# `expand_path` is the only sanctioned way a catalog names a filesystem location,
# so its rejections are the security boundary: a catalog is data, and a path value
# in it must not be able to reach outside the root it names.


def _expand(template, **roots):
    return config_loader.expand_path(
        template, roots, filename="sample.yaml", dotted_path="paths[0]"
    )


def test_a_token_expands_against_the_root_the_caller_supplied(tmp_path):
    root = tmp_path / "src"
    root.mkdir()

    assert _expand("<src_dir>/EWMRS/colormaps.json", src_dir=root) == (
        root / "EWMRS" / "colormaps.json"
    ).resolve()


@pytest.mark.parametrize("template", [
    "<src_dir>/../secrets.json",
    "<src_dir>/a/../../b",
    "<src_dir>//etc/passwd",
    "<src_dir>/",
])
def test_traversal_out_of_the_named_root_is_rejected(tmp_path, template):
    """The catalog may name a location below its root and nowhere else.

    Rejected textually, before any path is built, so a hostile value cannot even
    be used to probe for what exists.
    """
    with pytest.raises(config_loader.ConfigError, match="not a relative path below"):
        _expand(template, src_dir=tmp_path)


def test_a_bare_relative_path_is_rejected_rather_than_resolved_against_the_cwd(tmp_path):
    """The defect the tokens replace: `Path.cwd()` made the answer depend on argv[0]'s directory."""
    with pytest.raises(config_loader.ConfigError, match="must begin with one of"):
        _expand("colormaps.json", src_dir=tmp_path)


def test_a_token_outside_the_allowlist_is_rejected(tmp_path):
    """An allowlist, so a plausible-looking token fails loudly instead of expanding to itself."""
    with pytest.raises(config_loader.ConfigError, match="<repo_dir> is not an expandable path token"):
        _expand("<repo_dir>/config", src_dir=tmp_path)


def test_an_allowlisted_token_with_no_value_here_is_rejected(tmp_path):
    """`<base_dir>` is allowlisted but meaningless to a caller that has no base directory."""
    with pytest.raises(config_loader.ConfigError, match="<base_dir> has no value in this context"):
        _expand("<base_dir>/data", src_dir=tmp_path)


def test_a_backslash_template_is_reported_as_a_separator_problem(tmp_path):
    r"""Catalog paths are posix-style on every platform.

    Worth its own message because on Windows `<src_dir>\..\x` would otherwise be
    one opaque filename rather than the traversal it is.
    """
    with pytest.raises(config_loader.ConfigError, match="must use '/' separators"):
        _expand(r"<src_dir>\EWMRS\colormaps.json", src_dir=tmp_path)


def test_a_non_string_path_value_names_its_key(tmp_path):
    with pytest.raises(config_loader.ConfigError, match=r"sample\.yaml: paths\[0\]: expected a path string"):
        _expand(None, src_dir=tmp_path)


def test_a_symlink_pointing_out_of_the_root_is_rejected(tmp_path):
    """What the textual check cannot see, which is why both checks exist."""
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "escape").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation requires privileges not held here")

    with pytest.raises(config_loader.ConfigError, match="resolves outside <src_dir>"):
        _expand("<src_dir>/escape/passwd", src_dir=root)


@pytest.mark.parametrize("root", ["", ".", "relative/dir"])
def test_a_root_that_is_not_an_absolute_directory_is_rejected(root):
    """Otherwise `Path.resolve()` silently falls back to the working directory.

    This is the defect the mandatory leading token exists to prevent, arriving
    through the other argument: `Path("").resolve()` is the cwd, so an empty root
    produced a plausible path that moved with the launch directory. Caught here
    rather than trusted from the caller because the roots come from `util.file`'s
    import-time bind, the least observable point in the process.
    """
    with pytest.raises(config_loader.ConfigError, match="must be an absolute directory"):
        _expand("<src_dir>/colormaps.json", src_dir=root)


def test_a_nul_byte_in_a_path_is_rejected(tmp_path):
    """On Windows a NUL renders as a space, so this would reach a real other file.

    Rejected here rather than left to the filesystem call: `expand_path` returns a
    path that may be stored, compared, or logged long before anything opens it, and
    the eventual error would name neither the catalog nor the key.
    """
    with pytest.raises(config_loader.ConfigError, match="NUL byte"):
        _expand("<src_dir>/color\x00maps.json", src_dir=tmp_path)


def test_loaded_config_names_lists_only_the_catalogs_read_from_that_root(config_dir, tmp_path):
    """A startup summary must describe a process without loading 19 catalogs.

    `get_provenance` loads on a cache miss, so iterating `CONFIG_NAMES` to
    report paths would parse and schema-validate every catalog as a side effect
    of describing them. The answer is per root because the cache is keyed by
    root: a name loaded under one root must not be reported under another.
    """
    schema = {"type": "object", "additionalProperties": False, "properties": {"a": {"type": "integer"}}}
    _write(config_dir, "sample", {"a": 1}, schema)
    other_root = _config_dir(tmp_path / "other")
    _write(other_root, "sample", {"a": 2}, schema)

    config_loader.load_config("sample", config_dir=other_root)

    assert config_loader.loaded_config_names(config_dir=config_dir) == ()
    assert config_loader.loaded_config_names(config_dir=other_root) == ("sample",)

    config_loader.load_config("sample", config_dir=config_dir)

    assert config_loader.loaded_config_names(config_dir=config_dir) == ("sample",)
