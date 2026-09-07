"""Safe noninteractive implementation of ``edgewarn configure``."""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import stat
import tempfile
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from edgewarn_cli.config_path import resolve_config_root


class ConfigureError(ValueError):
    """An invalid configuration assignment (exit status 2)."""


class ConfigureIOError(OSError):
    """A configuration transaction I/O failure (exit status 1)."""


@dataclass(frozen=True)
class DottedTarget:
    name: str
    segments: tuple[str, ...]

    @property
    def dotted_path(self) -> str:
        return format_segments(self.segments)


@dataclass(frozen=True)
class EditResult:
    filename: str
    dotted_path: str
    old_value: Any
    new_value: Any


def add_configure_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "configure",
        help="safely modify an EdgeWARN configuration leaf",
    )
    parser.add_argument(
        "--config-path",
        type=Path,
        default=None,
        help="complete configuration directory (default: registered config root)",
    )
    parser.add_argument("target", nargs="?", metavar="FILE.KEY[.KEY|.INDEX...]")
    parser.add_argument("value", nargs="?", metavar="VALUE")
    parser.set_defaults(handler=configure_from_namespace, parser=parser)


def format_segments(segments: Sequence[str]) -> str:
    """Render path segments with literal dots and backslashes escaped."""
    return ".".join(segment.replace("\\", "\\\\").replace(".", "\\.") for segment in segments)


def _split_escaped_path(value: str) -> tuple[str, ...]:
    segments: list[str] = []
    current: list[str] = []
    escaped = False
    for character in value:
        if escaped:
            if character not in {".", "\\"}:
                raise ConfigureError(f"unsupported path escape \\{character}")
            current.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == ".":
            if not current:
                raise ConfigureError("configuration path contains an empty segment")
            segments.append("".join(current))
            current = []
        else:
            current.append(character)
    if escaped:
        raise ConfigureError("configuration path ends with an incomplete escape")
    if not current:
        raise ConfigureError("configuration path contains an empty segment")
    segments.append("".join(current))
    return tuple(segments)


def parse_dotted_target(value: str, config_names: Sequence[str]) -> DottedTarget:
    if not value:
        raise ConfigureError("configuration path contains an empty segment")
    if ".yaml." in value or value.endswith(".yaml"):
        raise ConfigureError("use a filename stem without .yaml")
    parts = _split_escaped_path(value)
    name, segments = parts[0], parts[1:]
    if name not in config_names:
        raise ConfigureError(
            f"unknown configuration name {name!r}; use a filename stem without .yaml"
        )
    if not segments:
        raise ConfigureError("replacing a configuration document root is not allowed")
    return DottedTarget(name=name, segments=tuple(segments))


def resolve_leaf(document: Any, segments: Sequence[str]) -> tuple[Any, str | int]:
    """Return the parent and final key/index for an existing dotted leaf."""
    current = document
    for position, segment in enumerate(segments):
        final = position == len(segments) - 1
        key: str | int
        if isinstance(current, Mapping):
            key = segment
            if key not in current:
                raise ConfigureError(f"missing mapping key at {'.'.join(segments[:position + 1])}")
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes, bytearray)):
            if segment.startswith("-"):
                raise ConfigureError("negative sequence indices are not allowed")
            if not segment.isdecimal():
                raise ConfigureError(
                    f"expected a decimal sequence index at {'.'.join(segments[:position + 1])}"
                )
            key = int(segment, 10)
            if key >= len(current):
                raise ConfigureError(f"sequence index out of range at {'.'.join(segments[:position + 1])}")
        else:
            raise ConfigureError(
                f"cannot traverse scalar at {'.'.join(segments[:position])}"
            )
        if final:
            return current, key
        current = current[key]
    raise ConfigureError("replacing a configuration document root is not allowed")


def parse_scalar(value: str) -> Any:
    """Parse exactly one untagged, unanchored YAML scalar."""
    import yaml
    from yaml.nodes import ScalarNode
    from yaml.tokens import AliasToken, AnchorToken, TagToken

    try:
        tokens = tuple(yaml.scan(value))
        if any(isinstance(token, (AliasToken, AnchorToken, TagToken)) for token in tokens):
            raise ConfigureError("VALUE must not contain YAML tags, anchors, or aliases")
        nodes = list(yaml.compose_all(value, Loader=yaml.SafeLoader))
        if len(nodes) != 1 or nodes[0] is None:
            raise ConfigureError("VALUE must contain exactly one YAML document")
        if not isinstance(nodes[0], ScalarNode):
            raise ConfigureError("VALUE must be a YAML scalar, not a collection")
        return yaml.safe_load(value)
    except ConfigureError:
        raise
    except yaml.YAMLError as exc:
        raise ConfigureError(f"invalid YAML scalar: {exc}") from exc


@contextlib.contextmanager
def _configuration_lock(root: Path) -> Iterator[None]:
    lock_path = root / ".edgewarn-config.lock"
    try:
        handle = lock_path.open("a+b")
    except OSError as exc:
        raise ConfigureIOError(f"cannot open configuration lock {lock_path}: {exc}") from exc
    locked = False
    try:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
        except OSError as exc:
            raise ConfigureIOError(
                f"cannot acquire configuration lock {lock_path}: {exc}"
            ) from exc
        yield
    finally:
        try:
            if locked and os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            elif locked:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _safe_target(root: Path, name: str) -> Path:
    requested = root / f"{name}.yaml"
    try:
        target = requested.resolve(strict=True)
    except OSError as exc:
        raise ConfigureError(f"{name}.yaml: cannot resolve target: {exc}") from exc
    if not target.is_relative_to(root):
        raise ConfigureError(f"{name}.yaml resolves outside configuration root")
    if not target.is_file():
        raise ConfigureError(f"{name}.yaml is not a regular file")
    return target


def _round_trip_yaml():
    try:
        from ruamel.yaml import YAML
    except ImportError as exc:
        raise ConfigureIOError(
            "round-trip YAML support is unavailable; install the declared ruamel.yaml dependency"
        ) from exc
    processor = YAML(typ="rt")
    processor.preserve_quotes = True
    return processor


def _load_document(target: Path) -> tuple[Any, bytes, int, bool]:
    try:
        original = target.read_bytes()
        mode = stat.S_IMODE(target.stat().st_mode)
        text = original.decode("utf-8")
        processor = _round_trip_yaml()
        document = processor.load(text)
    except ConfigureIOError:
        raise
    except Exception as exc:
        # ruamel's exception hierarchy is optional at import time.
        if isinstance(exc, OSError):
            raise ConfigureIOError(f"cannot read {target}: {exc}") from exc
        raise ConfigureError(f"{target.name}: malformed YAML: {exc}") from exc
    if not isinstance(document, MutableMapping):
        raise ConfigureError(f"{target.name}: top-level document must be a mapping")
    return document, original, mode, text.endswith("\n")


def _serialize(document: Any, *, final_newline: bool) -> bytes:
    stream = io.StringIO()
    _round_trip_yaml().dump(document, stream)
    text = stream.getvalue()
    if not final_newline:
        text = text.rstrip("\n")
    return text.encode("utf-8")


def _runtime_parse(content: bytes) -> Any:
    """Parse serialized bytes with the same YAML implementation as runtime."""
    import yaml

    return yaml.safe_load(content.decode("utf-8"))


def _write_temporary(fd: int, content: bytes) -> None:
    with os.fdopen(fd, "wb") as handle:
        written = handle.write(content)
        if written != len(content):
            raise OSError(f"short write: wrote {written} of {len(content)} bytes")
        handle.flush()
        os.fsync(handle.fileno())


def _atomic_replace(target: Path, content: bytes, mode: int) -> None:
    if mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) == 0:
        raise ConfigureIOError(f"target is read-only: {target}")
    temporary: Path | None = None
    fd: int | None = None
    try:
        fd, raw_path = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        temporary = Path(raw_path)
        _write_temporary(fd, content)
        fd = None
        os.chmod(temporary, mode)
        os.replace(temporary, target)
        temporary = None
    except OSError as exc:
        raise ConfigureIOError(f"atomic write failed for {target}: {exc}") from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def edit_configuration(
    config_root: str | os.PathLike[str], dotted_target: str | DottedTarget, scalar_text: str
) -> EditResult:
    """Apply one locked, validated, atomic configuration leaf edit."""
    from common.config import loader

    root = resolve_config_root(config_root)
    target_spec = (
        dotted_target
        if isinstance(dotted_target, DottedTarget)
        else parse_dotted_target(dotted_target, loader.CONFIG_NAMES)
    )
    if target_spec.name not in loader.CONFIG_NAMES:
        raise ConfigureError(f"unknown configuration name {target_spec.name!r}")
    new_value = parse_scalar(scalar_text)

    with _configuration_lock(root):
        target = _safe_target(root, target_spec.name)
        loader.reset_cache()
        loader.validate_all_configs(config_dir=root)
        document, original, mode, final_newline = _load_document(target)
        parent, key = resolve_leaf(document, target_spec.segments)
        old_value = parent[key]
        if isinstance(new_value, str):
            from ruamel.yaml.scalarstring import DoubleQuotedScalarString

            parent[key] = DoubleQuotedScalarString(new_value)
        else:
            parent[key] = new_value
        loader.validate_document(target_spec.name, document, config_dir=root)
        replacement = _serialize(document, final_newline=final_newline)
        serialized_document = _runtime_parse(replacement)
        loader.validate_document(
            target_spec.name, serialized_document, config_dir=root
        )

        _atomic_replace(target, replacement, mode)
        try:
            loader.reset_cache()
            loader.validate_all_configs(config_dir=root)
        except Exception as verification_error:
            try:
                _atomic_replace(target, original, mode)
                loader.reset_cache()
                loader.validate_all_configs(config_dir=root)
            except Exception as rollback_error:
                raise ConfigureIOError(
                    "post-write validation failed "
                    f"({verification_error}); rollback also failed ({rollback_error})"
                ) from rollback_error
            raise ConfigureIOError(
                f"post-write validation failed ({verification_error}); original file restored"
            ) from verification_error

    return EditResult(
        filename=f"{target_spec.name}.yaml",
        dotted_path=target_spec.dotted_path,
        old_value=old_value,
        new_value=new_value,
    )


def configure_from_namespace(args: argparse.Namespace) -> int:
    if args.target is None and args.value is None:
        if not os.sys.stdin.isatty() or not os.sys.stdout.isatty():
            args.parser.error(
                "interactive configuration requires TTY stdin and stdout; "
                "use FILE.KEY VALUE for a noninteractive edit"
            )

        from common.config import loader
        from yaml import YAMLError

        try:
            root = resolve_config_root(args.config_path)
            loader.reset_cache()
            loader.validate_all_configs(config_dir=root)
            from edgewarn_cli.tui import run_tui

            return run_tui(root, loader.CONFIG_NAMES)
        except (loader.ConfigError, ValueError, YAMLError) as exc:
            args.parser.error(str(exc))
        except (ConfigureIOError, OSError, ImportError) as exc:
            print(
                f"edgewarn configure: cannot start interactive editor: {exc}",
                file=os.sys.stderr,
            )
            return 1
    if args.target is None or args.value is None:
        args.parser.error("configure requires both FILE.KEY and VALUE")

    from common.config import loader
    from yaml import YAMLError

    try:
        root = resolve_config_root(args.config_path)
        result = edit_configuration(root, args.target, args.value)
    except (ConfigureError, loader.ConfigError, ValueError, YAMLError) as exc:
        args.parser.error(str(exc))
    except (ConfigureIOError, OSError) as exc:
        print(f"edgewarn configure: {exc}", file=os.sys.stderr)
        return 1

    print(f"file: {result.filename}")
    print(f"path: {result.dotted_path}")
    print(f"old: {result.old_value!r}")
    print(f"new: {result.new_value!r}")
    print("validation: passed")
    return 0
