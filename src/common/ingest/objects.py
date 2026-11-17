"""Transport-independent completion records for inventory/outbox producers."""
from dataclasses import dataclass
import hashlib
import json

from common.ingest.manifest import StagedInput


@dataclass(frozen=True)
class CommittedInput:
    record: StagedInput
    sha256: str
    source_locator: str
    remote_version: str | None = None
    reused: bool = False

    @property
    def input_id(self):
        # Equal validated bytes across mirrors or RAP reuse are one input.
        identity = (self.record.family, self.record.product,
                    self.record.analysis_time.isoformat(), self.sha256)
        return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
