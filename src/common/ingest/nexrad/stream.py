"""Stream boundary detection utilities for NEXRAD Level-II overlap-safe parsing.

Modeled after nexrad_overlap_scan_poc.py boundary semantics.
"""

from dataclasses import dataclass


VOLUME_MAGICS = (b"AR2V", b"ARCHIVE2")
MAX_MAGIC_OVERLAP = max(len(magic) for magic in VOLUME_MAGICS) - 1


@dataclass
class VolumeState:
    index: int
    volume_id: str
    file_path: str
    bytes_written: int = 0
    finalized: bool = False


def detect_next_volume_offset(
    previous_tail: bytes,
    payload: bytes,
    stream_has_started: bool,
    chunk_type: str | None = None,
) -> tuple[bool, int]:
    """Detect the next volume magic in the combined tail+payload stream.

    Returns (found, payload_offset) where payload_offset is the position
    within *payload* where the new volume magic begins, or -1 if not found.
    """
    if chunk_type is not None:
        # Level-II S chunks carry volume headers at offset zero. Magic bytes
        # inside compressed payloads are data, never a framing delimiter.
        found = chunk_type == "S" and stream_has_started and any(payload.startswith(magic) for magic in VOLUME_MAGICS)
        return (found, 0 if found else -1)
    search_space = previous_tail + payload
    best_offset = -1
    best_magic_len = 0

    for magic in VOLUME_MAGICS:
        offset = search_space.find(magic)
        if offset < 0:
            continue
        if not stream_has_started and offset == 0 and best_offset < 0:
            continue
        if offset < best_offset or best_offset < 0:
            best_offset = offset
            best_magic_len = len(magic)

    if best_offset < 0:
        return False, -1

    payload_offset = best_offset - len(previous_tail)
    return True, max(payload_offset, 0)


def split_at_boundary(
    payload: bytes,
    boundary_offset: int,
) -> tuple[bytes, bytes]:
    """Split payload into before/after at the detected boundary offset.

    before = payload[:boundary_offset]  (belongs to current scan)
    after  = payload[boundary_offset:]  (belongs to next scan)
    """
    before = payload[:boundary_offset]
    after = payload[boundary_offset:]
    return before, after
