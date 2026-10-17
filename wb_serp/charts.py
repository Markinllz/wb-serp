"""Pure chart construction; missing rank means NULL, never top_n+1."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
from collections.abc import Sequence

import plotly.graph_objects as go

MOSCOW = ZoneInfo("Europe/Moscow")
PALETTE = ("#5848E5", "#0C9D8D", "#F29E38", "#D6609F", "#3277CB", "#7649A4",
           "#E15759", "#55A630", "#7874CA", "#167F98")


def chart_rank_history(history: Sequence[dict], nm_ids: Sequence[int]) -> go.Figure:
    figure = go.Figure()
    for index, nm_id in enumerate(nm_ids):
        rows = [row for row in history if row["nm_id"] == nm_id]
        x = [datetime.fromisoformat(row["started_at"]).astimezone(MOSCOW) for row in rows]
        positions = [row["position"] for row in rows]
        depths = [row["requested_top_n"] for row in rows]
        figure.add_trace(go.Scatter(
            x=x, y=positions, name=str(nm_id), mode="lines+markers",
            connectgaps=False, line={"color": PALETTE[index % len(PALETTE)], "width": 2.5},
            marker={"size": 7, "line": {"width": 1.5, "color": "white"}},
            customdata=[[str(nm_id), depth] for depth in depths],
            hovertemplate="<b>Артикул %{customdata[0]}</b><br>Время: %{x|%d.%m.%Y %H:%M}"
                          "<br>Позиция: %{y}<br>Глубина: top-%{customdata[1]}<extra></extra>",
        ))
    figure.update_layout(
        template="plotly_white", height=475, hovermode="x",
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        margin={"l": 28, "r": 20, "t": 20, "b": 26},
        legend={"orientation": "h", "y": 1.14, "x": 0, "title_text": ""},
        xaxis={"title": "Дата и время · Москва", "showgrid": True, "gridcolor": "#EDF0F4"},
        yaxis={"title": "Позиция в выдаче", "autorange": "reversed", "dtick": 20,
               "showgrid": True, "gridcolor": "#EDF0F4"},
        font={"family": "Arial, sans-serif", "color": "#334155"},
    )
    return figure
