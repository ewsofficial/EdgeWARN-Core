"""Declarative discovery of installed CTAM modules from a manifest directory.

This replaces the hard-coded imports in ``src/EdgeWARN/ctam/modules/__init__.py``
with a one-level scan of an operator-owned directory. Nothing here executes,
imports, or launches module code -- Phase 4 owns launching. Discovery reads
``module.toml`` files, validates them, resolves the dependency order, and records
a verdict for every candidate it saw.

Recording every candidate is the point. The registry this replaces silently
overwrote a duplicate name (``src/EdgeWARN/ctam/registry.py``), so a shadowed
module vanished without a trace. Here an invalid, disabled, or over-capacity
module still appears in the result with a reason, which is what the status record
publishes for an operator to act on.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .limits import MAX_EXTERNAL_MODULES, RESERVED_MODULE_IDS
from .manifest import ManifestError, ModuleManifest, parse_manifest

MANIFEST_FILENAME = "module.toml"

STATE_DISCOVERED = "discovered"
STATE_INVALID = "invalid"
STATE_SKIPPED_DISABLED = "skipped_disabled"


@dataclass(frozen=True)
class DiscoveredModule:
    """One candidate directory and what discovery decided about it.

    ``manifest`` is retained for a disabled module -- an operator listing modules
    wants to see the version of something they turned off -- and is ``None`` only
    when the manifest could not be parsed at all.
    """

    module_id: str
    directory: Path
    manifest: ModuleManifest | None
    state: str
    reason: str | None


@dataclass(frozen=True)
class DiscoveryResult:
    """Every candidate found under ``root``, in stable execution order."""

    root: Path
    root_present: bool
    modules: tuple[DiscoveredModule, ...]

    @property
    def runnable(self) -> tuple[DiscoveredModule, ...]:
        """The modules that may proceed, in order. Empty is a normal outcome."""
        return tuple(m for m in self.modules if m.state == STATE_DISCOVERED)


def discover_modules(
    root: Path | str | None = None, *, config_dir: str | None = None
) -> DiscoveryResult:
    """Scan ``root`` one level deep for module manifests.

    A missing root is an empty external module set rather than a startup failure:
    an operator who installs no modules is running a supported configuration. A
    root that is a regular file is a misconfiguration and raises
    ``NotADirectoryError``, because silently treating it as empty would hide a
    typo that disables every module.
    """
    if root is None:
        from util.ctam_config import resolve_ctam_module_dir

        root = resolve_ctam_module_dir(config_dir=config_dir)
    root = Path(root)

    if not root.exists():
        return DiscoveryResult(root=root, root_present=False, modules=())
    if not root.is_dir():
        raise NotADirectoryError(
            f"CTAM module root {root} is a regular file; it must be a directory "
            f"containing one subdirectory per installed module"
        )

    candidates = _scan(root)
    candidates = _reject_case_collisions(candidates)
    candidates = _reject_disabled(candidates)
    ordered = _order(candidates)
    ordered = _apply_capacity(ordered)
    return DiscoveryResult(root=root, root_present=True, modules=tuple(ordered))


def _scan(root: Path) -> list[DiscoveredModule]:
    """One level deep, sorted by directory name so the input order is fixed."""
    resolved_root = root.resolve()
    found: list[DiscoveredModule] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        name = entry.name
        if name.startswith(".") or name.startswith("_"):
            continue
        if not entry.is_dir():
            continue
        manifest_path = entry / MANIFEST_FILENAME
        if not manifest_path.is_file():
            # An operator may keep a virtualenv, a data folder, or notes here.
            # Only a directory that claims to be a module is reported on.
            continue

        resolved_entry = entry.resolve()
        if resolved_entry != resolved_root and resolved_root not in resolved_entry.parents:
            found.append(
                DiscoveredModule(
                    module_id=name,
                    directory=entry,
                    manifest=None,
                    state=STATE_INVALID,
                    reason=(
                        f"module directory {name!r} resolves to {resolved_entry}, "
                        f"outside the module root {resolved_root}; a symlinked "
                        f"module folder is not followed"
                    ),
                )
            )
            continue

        try:
            manifest = parse_manifest(manifest_path)
        except ManifestError as exc:
            found.append(
                DiscoveredModule(
                    module_id=name,
                    directory=entry,
                    manifest=None,
                    state=STATE_INVALID,
                    reason=str(exc),
                )
            )
            continue
        found.append(
            DiscoveredModule(
                module_id=manifest.module_id,
                directory=entry,
                manifest=manifest,
                state=STATE_DISCOVERED,
                reason=None,
            )
        )
    return found


def _reject_case_collisions(candidates: list[DiscoveredModule]) -> list[DiscoveredModule]:
    """Two ids differing only by case are both invalid, never one winner.

    Because ``id`` must equal its directory name, a case-sensitive filesystem
    cannot produce two identical ids. It can produce ``CellStats`` and
    ``cellstats``, which collide once anything keys modules case-insensitively.
    Picking a winner is what the old registry did; naming both is what an
    operator can fix.
    """
    groups: dict[str, list[DiscoveredModule]] = {}
    for candidate in candidates:
        groups.setdefault(candidate.module_id.casefold(), []).append(candidate)

    result: list[DiscoveredModule] = []
    for candidate in candidates:
        group = groups[candidate.module_id.casefold()]
        if len(group) == 1:
            result.append(candidate)
            continue
        others = sorted(m.directory.name for m in group if m is not candidate)
        result.append(
            _invalidate(
                candidate,
                f"module id {candidate.module_id!r} collides case-insensitively "
                f"with directory {', '.join(repr(o) for o in others)}; module ids "
                f"must be unique ignoring case, so rename one of them",
            )
        )
    return result


def _reject_disabled(candidates: list[DiscoveredModule]) -> list[DiscoveredModule]:
    result: list[DiscoveredModule] = []
    for candidate in candidates:
        if candidate.state == STATE_DISCOVERED and not candidate.manifest.enabled:
            result.append(
                DiscoveredModule(
                    module_id=candidate.module_id,
                    directory=candidate.directory,
                    manifest=candidate.manifest,
                    state=STATE_SKIPPED_DISABLED,
                    reason=(
                        f"module {candidate.module_id!r} sets enabled = false in its "
                        f"manifest; set it to true to run it"
                    ),
                )
            )
            continue
        result.append(candidate)
    return result


def _invalidate(candidate: DiscoveredModule, reason: str) -> DiscoveredModule:
    return DiscoveredModule(
        module_id=candidate.module_id,
        directory=candidate.directory,
        manifest=candidate.manifest,
        state=STATE_INVALID,
        reason=reason,
    )


def _order(candidates: list[DiscoveredModule]) -> list[DiscoveredModule]:
    """Validate dependencies, then sort topologically with ties broken by id.

    The sort is a Kahn pass over a *sorted* ready set rather than a comparison
    sort over a partial order: ``after`` is not a total order, so a comparison
    sort would give an order that depends on the input sequence. The result here
    is a function of the dependency graph and the ids alone.

    ``stormprob`` is a legal dependency and always precedes external modules; the
    legacy ``stormcast`` dependency remains legal during the migration window, but
    both are built-in adapters and are never members of the result.
    """
    by_id = {c.module_id: c for c in candidates}
    runnable = {c.module_id: c for c in candidates if c.state == STATE_DISCOVERED}

    # Missing, unusable, or self-blocked dependencies. Iterated to a fixed point
    # so that invalidating one module also invalidates whatever depended on it,
    # instead of letting a transitive dependent look runnable and fail at runtime.
    invalid_reasons: dict[str, str] = {}
    while True:
        newly_invalid: dict[str, str] = {}
        for module_id, candidate in runnable.items():
            if module_id in invalid_reasons:
                continue
            for dependency in candidate.manifest.after:
                if dependency in RESERVED_MODULE_IDS:
                    continue
                other = by_id.get(dependency)
                if other is None:
                    newly_invalid[module_id] = (
                        f"module {module_id!r} declares after = [...{dependency!r}...] "
                        f"but no module with that id is installed in the module root; "
                        f"install it or remove the dependency"
                    )
                    break
                if other.state == STATE_SKIPPED_DISABLED:
                    newly_invalid[module_id] = (
                        f"module {module_id!r} depends on {dependency!r}, which is "
                        f"disabled; enable {dependency!r} or remove the dependency"
                    )
                    break
                if other.state == STATE_INVALID or dependency in invalid_reasons:
                    newly_invalid[module_id] = (
                        f"module {module_id!r} depends on {dependency!r}, which is "
                        f"invalid; fix {dependency!r} first"
                    )
                    break
        if not newly_invalid:
            break
        invalid_reasons.update(newly_invalid)

    for module_id, reason in invalid_reasons.items():
        runnable.pop(module_id, None)

    # Kahn over the remaining set. Anything left with a nonzero in-degree at the
    # end is in a cycle.
    edges = {
        module_id: {
            dependency
            for dependency in candidate.manifest.after
            if dependency in runnable
        }
        for module_id, candidate in runnable.items()
    }
    ready = sorted(module_id for module_id, deps in edges.items() if not deps)
    ordered_ids: list[str] = []
    remaining = dict(edges)
    while ready:
        module_id = ready.pop(0)
        ordered_ids.append(module_id)
        del remaining[module_id]
        newly_ready = []
        for other_id, deps in remaining.items():
            if module_id in deps:
                deps.discard(module_id)
                if not deps:
                    newly_ready.append(other_id)
        # Re-sorted rather than appended: the ready set is a set, so the order it
        # is drained in must come from the ids, not from discovery order.
        ready = sorted(ready + newly_ready)

    cycle_reasons: dict[str, str] = {}
    if remaining:
        members = ", ".join(repr(m) for m in sorted(remaining))
        for module_id in remaining:
            cycle_reasons[module_id] = (
                f"module {module_id!r} is part of an 'after' dependency cycle among "
                f"{members}; dependencies must form an acyclic graph, so break the "
                f"cycle"
            )

    ordered: list[DiscoveredModule] = [by_id[module_id] for module_id in ordered_ids]

    # Everything that has no place in the dependency order still appears, sorted
    # by id at the end, so the status record can report it.
    trailing: list[DiscoveredModule] = []
    for candidate in candidates:
        module_id = candidate.module_id
        if module_id in ordered_ids:
            continue
        if module_id in cycle_reasons:
            trailing.append(_invalidate(candidate, cycle_reasons[module_id]))
        elif module_id in invalid_reasons:
            trailing.append(_invalidate(candidate, invalid_reasons[module_id]))
        else:
            trailing.append(candidate)
    trailing.sort(key=lambda m: m.module_id)
    return ordered + trailing


def _apply_capacity(ordered: list[DiscoveredModule]) -> list[DiscoveredModule]:
    """Demote runnable modules past the cap, after ordering rather than before.

    The limits document ties the cap to "stable dependency-then-ID order", so the
    order has to exist before the tail can be identified. Which module is dropped
    is then a property of the installed set, not of directory iteration order.
    StormCast is not counted: it is the built-in, not an installed module.
    """
    result: list[DiscoveredModule] = []
    admitted = 0
    for candidate in ordered:
        if candidate.state != STATE_DISCOVERED:
            result.append(candidate)
            continue
        admitted += 1
        if admitted <= MAX_EXTERNAL_MODULES:
            result.append(candidate)
            continue
        result.append(
            _invalidate(
                candidate,
                f"module {candidate.module_id!r} is number {admitted} in stable "
                f"dependency-then-id order, past the maximum of "
                f"{MAX_EXTERNAL_MODULES} external modules; uninstall or disable a "
                f"module to make room",
            )
        )
    return result
