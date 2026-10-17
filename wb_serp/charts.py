"""Pure chart construction; missing rank means NULL, never top_n+1."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
from collections.abc import Sequence
import math

import plotly.graph_objects as go

MOSCOW = ZoneInfo("Europe/Moscow")
PALETTE = ("#2456A6", "#187A68", "#BA691F", "#AC4881", "#3277CB", "#7649A4",
           "#E15759", "#55A630", "#7874CA", "#167F98")


def chart_rank_history(history: Sequence[dict], nm_ids: Sequence[int]) -> go.Figure:
    figure = go.Figure()
    for index, nm_id in enumerate(nm_ids):
        rows = [row for row in history if row["nm_id"] == nm_id]
        x = [datetime.fromisoformat(row["started_at"]).astimezone(MOSCOW) for row in rows]
        positions = [row["position"] for row in rows]
        depths = [row["requested_top_n"] for row in rows]
        partial = [row.get("run_status") == "partial" for row in rows]
        figure.add_trace(go.Scatter(
            x=x, y=positions, name=str(nm_id), mode="lines+markers",
            connectgaps=False, line={"color": PALETTE[index % len(PALETTE)], "width": 2.5},
            marker={"size": 8, "symbol": ["diamond-open" if p else "circle" for p in partial]},
            customdata=[[str(nm_id), depth, "Неполный сбор" if p else "Полный сбор"]
                        for depth, p in zip(depths, partial, strict=True)],
            hovertemplate="<b>Артикул %{customdata[0]}</b><br>Время: %{x|%d.%m.%Y %H:%M}"
                          "<br>Позиция: %{y}<br>Топ-%{customdata[1]} · %{customdata[2]}<extra></extra>",
        ))
    positions = [row["position"] for row in history if row["position"] is not None]
    span = max(positions) - min(positions) if positions else 0
    raw_step = max(1, span / 7)
    magnitude = 10 ** math.floor(math.log10(raw_step))
    step = next(n * magnitude for n in (1, 2, 5, 10) if n * magnitude >= raw_step)
    figure.update_layout(
        template="plotly_white", height=390, hovermode="x",
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        margin={"l": 16, "r": 16, "t": 40, "b": 16},
        legend={"orientation": "h", "y": 1.14, "x": 0, "title_text": ""},
        xaxis={"title": "Время, МСК", "showgrid": False, "tickformat": "%d.%m\n%H:%M"},
        yaxis={"title": "Позиция", "autorange": "reversed", "dtick": step, "tickformat": "d",
               "autorangeoptions": {"clipmin": 0.5},
               "showgrid": True, "gridcolor": "#EDF0F4"},
        font={"family": "Arial, sans-serif", "color": "#334155"},
    )
    return figure
