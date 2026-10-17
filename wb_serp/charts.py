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
                          color_indices: dict[int, int] | None = None,
                          price_field: str = "price_kopecks") -> go.Figure:
    if metric not in ("position", "price"):
        raise ValueError("Unknown history metric")
    if price_field not in ("price_kopecks", "card_price_kopecks"):
        raise ValueError("Unknown price field")
    rank = metric == "position"
    figure = go.Figure()
    for index, nm_id in enumerate(nm_ids):
        color_index = (color_indices or {}).get(nm_id, index)
        raw_title = (titles or {}).get(nm_id, f'Артикул {nm_id}')
        title = html.escape(raw_title)
        short_title = html.escape(raw_title[:22]) + ("…" if len(raw_title) > 22 else "")
        rows = [row for row in history if row["nm_id"] == nm_id]
        x = [datetime.fromisoformat(row["started_at"]).astimezone(MOSCOW) for row in rows]
        values = [row["position"] if rank else
                  row[price_field] / 100 if row.get(price_field) is not None else None
                  for row in rows]
        depths = [row["requested_top_n"] for row in rows]
        partial = [row.get("run_status") == "partial" for row in rows]
        figure.add_trace(go.Scatter(
            x=x, y=values, name=(f"{nm_id}: {short_title}" if titles else str(nm_id)),
            meta={"nm_id": nm_id}, mode="lines+markers",
            connectgaps=False, line={"color": PALETTE[color_index % len(PALETTE)], "width": 2.7,
                                    "dash": ('solid', 'dash', 'dot')[(color_index // len(PALETTE)) % 3]},
            marker={"size": 8, "symbol": ["diamond-open" if p else "circle" for p in partial]},
            customdata=[[str(nm_id), depth, "Неполный сбор" if p else "Полный сбор", title]
                        for depth, p in zip(depths, partial, strict=True)],
            hovertemplate="<b>%{customdata[3]}</b><br>Артикул %{customdata[0]}<br>Время: %{x|%d.%m.%Y %H:%M}"
                          + ("<br>Позиция: %{y}" if rank else "<br>Цена: %{y:.2f} ₽")
                          + "<br>Топ-%{customdata[1]}<br>%{customdata[2]}<extra></extra>",
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

    def label(delta: int) -> str:
        n = abs(delta)
        unit = "мест" if 11 <= n % 100 <= 14 else (
            "место" if n % 10 == 1 else "места" if 2 <= n % 10 <= 4 else "мест")
        return f"{'↑' if delta > 0 else '↓'} {n} {unit}"

    figure = go.Figure(go.Bar(
        x=[p["delta"] for p in changed], y=[str(p["nm_id"]) for p in changed], orientation="h",
        marker_color=["#187A68" if p["delta"] > 0 else "#AE4646" for p in changed],
        text=[label(p['delta']) for p in changed], textposition="none", cliponaxis=False,
        customdata=[[str(p["nm_id"]), p["before"], p["after"],
                     html.escape(p.get("title") or f"Артикул {p['nm_id']}")] for p in changed],
        hovertemplate="<b>%{customdata[3]}</b><br>Артикул %{customdata[0]}"
                      "<br>Позиция %{customdata[1]} → %{customdata[2]}<extra></extra>",
    ))
    figure.update_layout(
        template="plotly_white", height=max(260, len(changed) * 28 + 90), showlegend=False,
        margin={"l": 16, "r": 90, "t": 16, "b": 16},
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font={"family": "Arial, sans-serif", "color": "#334155"},
        xaxis={"title": "← Опустились                 Поднялись →", "zeroline": True,
               "zerolinecolor": "#8993A3", "gridcolor": "#EDF0F4", "tickformat": "+d"},
        yaxis={"title": None, "type": "category", "tickmode": "array",
               "tickvals": [str(p["nm_id"]) for p in changed],
               "ticktext": [f"{html.escape((p.get('title') or str(p['nm_id']))[:30])}"
                            f"<br>{p['nm_id']}  № {p['before']} → № {p['after']}" for p in changed],
               "automargin": True},
        annotations=[{"x": max(0, p["delta"]), "y": index, "xref": "x", "yref": "y",
                      "text": label(p["delta"]), "showarrow": False,
                      "xanchor": "left", "xshift": 6,
                      "font": {"size": 12, "color": "#AE4646" if p["delta"] < 0 else "#334155"}}
                     for index, p in enumerate(changed)],
    )
    return figure


def chart_turnover(rows: Sequence[dict]) -> go.Figure:
    """Share of new products relative to the preceding complete snapshot."""
    figure = go.Figure(go.Scatter(
        x=[datetime.fromisoformat(r["started_at"]).astimezone(MOSCOW) for r in rows],
        y=[r["percent"] for r in rows], mode="lines+markers", connectgaps=False,
        line={"color": "#2563EB", "width": 2.7}, marker={"size": 7},
        fill="tozeroy", fillcolor="rgba(37,99,235,.07)",
        customdata=[[r["entered"], r["depth"], r["retained"],
                     datetime.fromisoformat(r["previous_at"]).astimezone(MOSCOW).strftime("%d.%m %H:%M")]
                    for r in rows],
        hovertemplate="%{x|%d.%m.%Y %H:%M}<br>Новых товаров: %{customdata[0]} из %{customdata[1]}"
                      "<br>Остались в топе: %{customdata[2]}<br>Обновление: %{y:.1f}%"
                      "<br>Сравнение с %{customdata[3]}<extra></extra>",
    ))
    upper = min(100, max(10, math.ceil(max((r["percent"] for r in rows), default=0) / 10) * 10))
    figure.update_layout(
        template="plotly_white", height=320, showlegend=False,
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        margin={"l": 16, "r": 16, "t": 12, "b": 16},
        font={"family": "Arial, sans-serif", "color": "#334155"},
        xaxis={"title": "Время, МСК", "showgrid": False, "tickformat": "%d.%m\n%H:%M"},
        yaxis={"title": "Новые товары, %", "range": [0, upper], "ticksuffix": "%",
               "gridcolor": "#EDF0F4"},
    )
    return figure
