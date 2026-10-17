"""View models shared by the dashboard and UI checks."""

from datetime import datetime, timezone


def next_collection(now: datetime, period: int = 3600) -> datetime:
    if now.tzinfo is None or period < 1:
        raise ValueError("An aware time and positive period are required")
    return datetime.fromtimestamp((int(now.timestamp()) // period + 1) * period, timezone.utc)


def product_summaries(history: list[dict], selected: list[int], *, metric: str,
                      price_field: str = "price_kopecks") -> list[dict]:
    field = "position" if metric == "position" else price_field
    result = []
    for nm_id in selected:
        rows = [r for r in history if r["nm_id"] == nm_id]
        if not rows:
            continue
        latest = rows[-1]
        value = latest.get(field)
        previous = rows[-2].get(field) if len(rows) > 1 else None
        delta = (
            None
            if value is None or previous is None
            else (previous - value if metric == "position" else value - previous)
        )
        absent = latest.get("position") is None
        result.append(
            {
                "nm_id": nm_id,
                "value": value,
                "delta": delta,
                "started_at": latest["started_at"],
                "state": "outside"
                if absent and latest["run_status"] == "success"
                else "unknown"
                if absent
                else "observed",
                "depth": latest["requested_top_n"],
            }
        )
    return result
