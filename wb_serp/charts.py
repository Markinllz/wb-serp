"""Pure chart construction; missing rank means NULL, never top_n+1."""
from __future__ import annotations

from datetime import datetime
import html
from zoneinfo import ZoneInfo
from collections.abc import Sequence
import math

import plotly.graph_objects as go

MOSCOW = ZoneInfo("Europe/Moscow")
PALETTE = ("#2563EB", "#0D9488", "#D97706", "#AC4881", "#3277CB", "#7649A4",
           "#E15759", "#55A630", "#7874CA", "#167F98")


def chart_rank_history(history: Sequence[dict], nm_ids: Sequence[int]) -> go.Figure:
    return chart_product_history(history, nm_ids, metric="position")


def chart_product_history(history: Sequence[dict], nm_ids: Sequence[int], *,
                          metric: str = "position", titles: dict[int, str] | None = None,
                          color_indices: dict[int, int] | None = None) -> go.Figure:
    if metric not in ("position", "price"):
        raise ValueError("Unknown history metric")
    rank = metric == "position"
    figure = go.Figure()
    for index, nm_id in enumerate(nm_ids):
        color_index = (color_indices or {}).get(nm_id, index)
        title = html.escape((titles or {}).get(nm_id, f'Артикул {nm_id}'))
        rows = [row for row in history if row["nm_id"] == nm_id]
        x = [datetime.fromisoformat(row["started_at"]).astimezone(MOSCOW) for row in rows]
        values = [row["position"] if rank else
                  row["price_kopecks"] / 100 if row.get("price_kopecks") is not None else None
                  for row in rows]
        depths = [row["requested_top_n"] for row in rows]
        partial = [row.get("run_status") == "partial" for row in rows]
        figure.add_trace(go.Scatter(
            x=x, y=values, name=(f"{nm_id} · {title[:32]}" if titles else str(nm_id)),
            meta={"nm_id": nm_id}, mode="lines+markers",
            connectgaps=False, line={"color": PALETTE[color_index % len(PALETTE)], "width": 2.7,
                                    "dash": ('solid', 'dash', 'dot')[(color_index // len(PALETTE)) % 3]},
            marker={"size": 8, "symbol": ["diamond-open" if p else "circle" for p in partial]},
            customdata=[[str(nm_id), depth, "Неполный сбор" if p else "Полный сбор", title]
                        for depth, p in zip(depths, partial, strict=True)],
            hovertemplate="<b>%{customdata[3]}</b><br>Артикул %{customdata[0]}<br>Время: %{x|%d.%m.%Y %H:%M}"
                          + ("<br>Позиция: %{y}" if rank else "<br>Цена: %{y:.2f} ₽")
                          + "<br>Топ-%{customdata[1]} · %{customdata[2]}<extra></extra>",
        ))
    positions = [row["position"] for row in history if row["position"] is not None]
    span = max(positions) - min(positions) if positions else 0
    raw_step = max(1, span / 7)
    magnitude = 10 ** math.floor(math.log10(raw_step))
    step = next(n * magnitude for n in (1, 2, 5, 10) if n * magnitude >= raw_step)
    yaxis = ({"title": "Позиция", "autorange": "reversed", "dtick": step, "tickformat": "d",
              "autorangeoptions": {"clipmin": 0.5}} if rank else
             {"title": "Цена, ₽", "autorange": True, "tickformat": ",.0f"})
    figure.update_layout(
        template="plotly_white", height=430, hovermode="closest",
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        margin={"l": 16, "r": 16, "t": 40, "b": 16},
        legend={"orientation": "h", "y": 1.14, "x": 0, "title_text": ""},
        xaxis={"title": "Время, МСК", "showgrid": False, "tickformat": "%d.%m\n%H:%M",
               "showspikes": True, "spikecolor": "#b7c5d8", "spikethickness": 1},
        yaxis={**yaxis, "showgrid": True, "gridcolor": "#EDF0F4"},
        font={"family": "Arial, sans-serif", "color": "#334155"},
    )
    return figure


def chart_rank_changes(products: Sequence[dict], *, limit: int = 12) -> go.Figure:
    """Largest observed movements; never assign a delta to entries or exits."""
    if not 1 <= limit <= 30:
        raise ValueError("Invalid movement limit")
    changed = sorted((p for p in products if p["delta"] is not None and p["delta"] != 0),
                     key=lambda p: (-abs(p["delta"]), p["nm_id"]))[:limit]
    changed.sort(key=lambda p: (p["delta"], p["nm_id"]))
    figure = go.Figure(go.Bar(
        x=[p["delta"] for p in changed], y=[str(p["nm_id"]) for p in changed], orientation="h",
        marker_color=["#187A68" if p["delta"] > 0 else "#AE4646" for p in changed],
        text=[f'{p["delta"]:+d}' for p in changed], textposition="outside", cliponaxis=False,
        customdata=[[str(p["nm_id"]), p["before"], p["after"]] for p in changed],
        hovertemplate="Артикул %{customdata[0]}<br>Было: %{customdata[1]}"
                      "<br>Стало: %{customdata[2]}<br>Изменение: %{x:+d}<extra></extra>",
    ))
    figure.update_layout(
        template="plotly_white", height=max(260, len(changed) * 28 + 90), showlegend=False,
        margin={"l": 16, "r": 32, "t": 16, "b": 16},
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font={"family": "Arial, sans-serif", "color": "#334155"},
        xaxis={"title": "Изменение позиции · + выше, − ниже", "zeroline": True,
               "zerolinecolor": "#8993A3", "gridcolor": "#EDF0F4", "tickformat": "+d"},
        yaxis={"title": "Артикул", "type": "category"},
    )
    return figure
