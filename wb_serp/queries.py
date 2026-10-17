"""Read literal query phrases; don't canonicalize or fix spelling."""

from __future__ import annotations

from pathlib import Path


def read_queries(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    values = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]
    if not values:
        raise ValueError(f"No queries in {path}")
    if len(set(values)) != len(values):
        raise ValueError("Duplicate exact query phrases in source")
    return values
