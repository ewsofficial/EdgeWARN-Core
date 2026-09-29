"""Out-of-process execution for discovered external CTAM modules."""
from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Mapping, Sequence

from common.config.overlay import inherited_process_path

from .api import CTAMReadService, LoopbackCTAMServer
from .manifest import ModuleManifest
from .readiness import CTAMCycleCatalog, evaluate_requirements
from .transaction import CTAMTransactionService

MAX_CAPTURE_BYTES = 65_536


@dataclass(frozen=True)
class ModuleRunResult:
    module_id: str
    state: str
    duration_seconds: float
    returncode: int | None
    stdout: str
    stderr: str
    reason: str | None = None


class ExternalModuleRunner:
    """Run a fixed, discovered manifest list without a shell or broad cleanup."""
    def __init__(self, *, catalog: CTAMCycleCatalog, cells: Sequence[dict], manifests: Mapping[str, ModuleManifest], histories: Mapping[str, Sequence[dict]] | None = None) -> None:
        self.catalog, self.manifests = catalog, dict(manifests)
        self.transactions = CTAMTransactionService(cells=cells, histories=histories, manifests=self.manifests)
        self.service = CTAMReadService(catalog=catalog, cells=cells, manifests=self.manifests, transactions=self.transactions)

    @staticmethod
    def _argv(manifest: ModuleManifest) -> list[str]:
        return [sys.executable if arg == "{python}" else arg for arg in manifest.entrypoint]

    @staticmethod
    def _capture(data: bytes | None) -> str:
        data = data or b""
        suffix = b"\n[output truncated]" if len(data) > MAX_CAPTURE_BYTES else b""
        return (data[:MAX_CAPTURE_BYTES] + suffix).decode("utf-8", "replace")

    def run(self) -> tuple[ModuleRunResult, ...]:
        results: list[ModuleRunResult] = []
        with LoopbackCTAMServer(self.service) as server:
            for module_id, manifest in self.manifests.items():
                evaluation = evaluate_requirements(manifest, self.catalog)
                if not evaluation["satisfied"]:
                    results.append(ModuleRunResult(module_id, "skipped_missing_requirements", 0, None, "", "", "declared requirements are not satisfied")); continue
                started = time.monotonic()
                checkpoint = self.transactions.checkpoint()
                env = {"PATH": inherited_process_path(), "CTAM_API_URL": server.url, "CTAM_API_TOKEN": server.token_for(module_id), "CTAM_CYCLE_ID": self.catalog.cycle_id, "CTAM_MODULE_ID": module_id}
                try:
                    process = subprocess.Popen(self._argv(manifest), cwd=manifest.directory, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    try:
                        stdout, stderr = process.communicate(timeout=manifest.timeout_seconds)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        try: stdout, stderr = process.communicate(timeout=2)
                        except subprocess.TimeoutExpired:
                            process.kill(); stdout, stderr = process.communicate()
                        results.append(ModuleRunResult(module_id, "timed_out", time.monotonic() - started, process.returncode, self._capture(stdout), self._capture(stderr), "module exceeded manifest timeout")); continue
                    violations = self.service.contract_violations(module_id)
                    if violations:
                        self.transactions.revoke_after_contract_violation(module_id, checkpoint)
                        results.append(ModuleRunResult(
                            module_id, "failed", time.monotonic() - started,
                            process.returncode, self._capture(stdout), self._capture(stderr),
                            "undeclared input access: " + ", ".join(violations)
                            + "; add matching [[requires]] selectors and restart",
                        )); continue
                    if process.returncode != 0:
                        results.append(ModuleRunResult(module_id, "failed", time.monotonic() - started, process.returncode, self._capture(stdout), self._capture(stderr), "module exited nonzero")); continue
                    if not self.transactions.transactions[module_id].sealed:
                        results.append(ModuleRunResult(module_id, "failed", time.monotonic() - started, process.returncode, self._capture(stdout), self._capture(stderr), "module exited without committing")); continue
                    results.append(ModuleRunResult(module_id, "completed", time.monotonic() - started, process.returncode, self._capture(stdout), self._capture(stderr)))
                except OSError as exc:
                    results.append(ModuleRunResult(module_id, "failed", time.monotonic() - started, None, "", "", f"launch failed: {exc}"))
        return tuple(results)
