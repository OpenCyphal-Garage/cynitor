"""Guess the type of a subject no register names, from payloads published on it.

A type fits only if every payload decodes as it and encodes back to exactly
the same bytes. Cyphal pads a short payload with zeros and drops the excess
of a long one, so a plain decode succeeds for almost any type; the round trip
does not, unless the length, the length prefixes and the padding all agree.
On CAN FD a frame has one of a few sizes, so a payload may arrive with the
zeros that filled its frame up; those, and no more, are allowed.

Types of the same size still all fit (four bytes are a Real32, a Natural32,
a temperature, a voltage, ...), so the fits are ranked, and shown with the
latest payload decoded as each, for the user to choose from:
  1. plausible values first: no NaN, infinity, or float far outside what
     sensors report (as a float read from an integer's bytes gives);
  2. the user's own types (custom DSDL) before the standard ones;
  3. types with a fixed subject-ID of their own last: they are seldom
     published elsewhere.
"""

from __future__ import annotations

import logging
import math
from typing import Any, NamedTuple

MAX_CANDIDATES = 100  # a four-byte float fits dozens of standard types
# Beyond these, a float is more likely other bytes read as one than a reading.
_TINY, _HUGE = 1e-12, 1e12


class Candidate(NamedTuple):
    name: str               # DSDL name, e.g. "uavcan.si.sample.temperature.Scalar.1.0"
    data_type: Any          # its compiled class
    custom: bool
    fixed_port: bool


def load_candidates(types: list[dict]) -> list[Candidate]:
    """The compiled classes of ``types`` (DSDLManager.message_types), skipping any that do not import."""
    from scanner_node import ScannerNode  # imports the compiled DSDL
    candidates = []
    for t in types:
        try:
            data_type = ScannerNode._message_class(ScannerNode._dsdl_type_to_module_name(t["full_name"]))
        except ValueError:
            continue
        candidates.append(Candidate(t["full_name"], data_type, t["custom"], t["fixed_port"]))
    return candidates


_FD_FRAME_SIZES = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)


def fd_padding_fits(encoded: int, received: int) -> bool:
    """Whether CAN FD frame padding explains ``received`` payload bytes for ``encoded`` ones."""
    if received + 1 <= 64:  # one frame: the payload and a tail byte, rounded up to a frame size
        return received + 1 == next(size for size in _FD_FRAME_SIZES if size >= encoded + 1)
    return received - encoded < 64  # several frames: the last one is padded


def exact_decode(data_type, payload: bytes, fd: bool = False):
    """``payload`` decoded as ``data_type`` if it encodes back to the same bytes, else None.

    With ``fd``, the zeros CAN FD frame padding adds may follow them.
    """
    import pycyphal.dsdl
    decoded = pycyphal.dsdl.deserialize(data_type, [memoryview(payload)])
    if decoded is None:
        return None
    encoded = b"".join(pycyphal.dsdl.serialize(decoded))
    if encoded == payload:
        return decoded
    padding = payload[len(encoded):]
    if fd and payload.startswith(encoded) and not any(padding) and fd_padding_fits(len(encoded), len(payload)):
        return decoded
    return None


def implausible_values(value: Any) -> int:
    """How many floats in a decoded message (as builtins) look like misread bytes."""
    if isinstance(value, dict):
        return sum(implausible_values(v) for v in value.values())
    if isinstance(value, list):
        return sum(implausible_values(v) for v in value)
    if isinstance(value, float):
        return int(not math.isfinite(value) or abs(value) > _HUGE or 0 < abs(value) < _TINY)
    return 0


def json_safe(value: Any) -> Any:
    """``value`` with non-finite floats as strings: JSON has no NaN."""
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def rank_types(payloads: list[bytes], candidates: list[Candidate], fd: bool = False) -> dict:
    """The candidates every payload fits, best first: {matches, candidates: [{type, custom, preview}]}."""
    import pycyphal.dsdl
    # It logs every payload a type does not fit, with a traceback: here, most of them.
    decoder_log = logging.getLogger("nunavut_support")
    level = decoder_log.level
    decoder_log.setLevel(logging.WARNING)
    try:
        fits = _fits(payloads, candidates, fd, pycyphal.dsdl.to_builtin)
    finally:
        decoder_log.setLevel(level)
    fits.sort(key=lambda fit: fit[0])
    return {"matches": len(fits), "candidates": [fit for _, fit in fits[:MAX_CANDIDATES]]}


def _fits(payloads: list[bytes], candidates: list[Candidate], fd: bool, to_builtin) -> list[tuple]:
    """(rank, candidate) for each candidate every payload fits."""
    fits = []
    for candidate in candidates:
        try:
            decoded = [exact_decode(candidate.data_type, payload, fd) for payload in payloads]
        except Exception:  # a type pycyphal cannot handle is simply not a fit
            continue
        if not decoded or any(d is None for d in decoded):
            continue
        builtins = [to_builtin(d) for d in decoded]
        odd = sum(implausible_values(b) for b in builtins)
        rank = (odd > 0, not candidate.custom, candidate.fixed_port, odd, candidate.name)
        fits.append((rank, {"type": candidate.name, "custom": candidate.custom,
                            "preview": json_safe(builtins[-1])}))
    return fits
