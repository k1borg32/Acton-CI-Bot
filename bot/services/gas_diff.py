"""
Gas-snapshot diff between two Acton test runs.

Snapshot shape (from `acton test --snapshot path.json`):

    {
      "timestamp": 1779019007,
      "opcodes": {
        "IncreaseCounter": {
          "min_gas": 843, "max_gas": 1554, "avg_gas": 1412,
          "samples": 5,  "all_values": [...]
        },
        ...
      },
      "trace_chains": { ... per-test traces, less useful for diffs ... }
    }

For PR diffs we compare the `opcodes` section by name (message types are
stable across PRs; individual test names rename more often).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GasDelta:
    """One opcode's gas change between base and head."""
    name: str
    base_avg: int | None   # None = opcode is new in head
    head_avg: int | None   # None = opcode was removed in head

    @property
    def delta_abs(self) -> int | None:
        if self.base_avg is None or self.head_avg is None:
            return None
        return self.head_avg - self.base_avg

    @property
    def delta_pct(self) -> float | None:
        if self.base_avg is None or self.head_avg is None or self.base_avg == 0:
            return None
        return (self.head_avg - self.base_avg) / self.base_avg * 100


def _load_opcodes(path: str) -> dict[str, int] | None:
    """Return {opcode_name: avg_gas} or None on parse failure."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Couldn't parse gas snapshot %s: %s", path, e)
        return None
    opcodes = data.get("opcodes") or {}
    if not isinstance(opcodes, dict):
        return None
    out: dict[str, int] = {}
    for name, stats in opcodes.items():
        if not isinstance(stats, dict):
            continue
        avg = stats.get("avg_gas")
        if isinstance(avg, (int, float)):
            out[name] = int(avg)
    return out


def diff_snapshots(base_path: str, head_path: str) -> list[GasDelta]:
    """Compute per-opcode deltas. Returns [] if either file is missing/empty."""
    base = _load_opcodes(base_path)
    head = _load_opcodes(head_path)
    if base is None or head is None:
        return []
    names = set(base) | set(head)
    deltas: list[GasDelta] = []
    for name in names:
        deltas.append(GasDelta(
            name=name,
            base_avg=base.get(name),
            head_avg=head.get(name),
        ))
    return deltas


def filter_significant(
    deltas: Iterable[GasDelta],
    *,
    min_abs_change: int = 10,
    min_pct_change: float = 1.0,
) -> list[GasDelta]:
    """Drop deltas below the noise floor (small absolute AND small %).

    New/removed opcodes always survive (delta_abs is None).
    """
    out: list[GasDelta] = []
    for d in deltas:
        if d.delta_abs is None:
            out.append(d)
            continue
        abs_change = abs(d.delta_abs)
        pct_change = abs(d.delta_pct or 0.0)
        if abs_change >= min_abs_change or pct_change >= min_pct_change:
            out.append(d)
    return out


def rank(deltas: Iterable[GasDelta]) -> list[GasDelta]:
    """Sort by absolute change magnitude (largest first). New/removed first."""
    def key(d: GasDelta) -> tuple[int, int]:
        # new/removed → priority 0 (top); others ranked by |Δ|
        if d.delta_abs is None:
            return (0, 0)
        return (1, -abs(d.delta_abs))
    return sorted(deltas, key=key)
