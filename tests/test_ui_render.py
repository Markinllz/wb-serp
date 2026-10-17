"""Headless UI flow tests. These test Streamlit calls and data wiring, not browser pixels.

The full Streamlit app is only browser/E2E testable in an environment with the UI
package installed and a reachable Docker/browser daemon.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
import importlib.util
import sys

import pytest

from wb_serp import config
from wb_serp.models import Product
from wb_serp.store import Store
from decimal import Decimal
from datetime import timezone
import csv
import io


class Stopped(Exception):
    pass


class Context:
    def __enter__(self): return self
    def __exit__(self, *args): return False


class FakeStreamlit(ModuleType):
    def __init__(self):
        super().__init__("streamlit")
        self.sidebar = Context()
        self.plots = []
        self.frames = []
        self.exports = []
        self.messages = []
        self.session_state = {}
        self.column_config = SimpleNamespace(NumberColumn=lambda **kw: kw, TextColumn=lambda **kw: kw)
    def set_page_config(self, **kwargs): self.page_config = kwargs
    def markdown(self, *args, **kwargs): pass
    def error(self, msg): self.messages.append(("error",msg))
    def info(self, msg): self.messages.append(("info",msg))
    def warning(self, msg): self.messages.append(("warning",msg))
    def stop(self): raise Stopped()
    def caption(self, *args, **kwargs): pass
    def divider(self): pass
    def columns(self, num): return [Context() for _ in range(num)]
    def container(self, **kwargs): return Context()
    def expander(self, *args, **kwargs): return Context()
    def tabs(self, names): return [Context() for _ in names]
    def metric(self, *args, **kwargs): pass
    def selectbox(self, *args, options, **kwargs): return options[0]
    def date_input(self, name, *, value, **kwargs): return value
    def button(self, *args, **kwargs): return False
    def text_input(self, *args, **kwargs): return ""
    def multiselect(self, *args, default=None, key=None, **kwargs):
        return self.session_state.get(key, default or [])
    def plotly_chart(self, chart, **kwargs): self.plots.append(chart)
    def dataframe(self, dataframe, **kwargs): self.frames.append(dataframe.copy())
    def download_button(self, label, data, *args, **kwargs): self.exports.append((label, data))


def load_dashboard(monkeypatch):
    mock = FakeStreamlit()
    monkeypatch.setitem(sys.modules, "streamlit", mock)
    # Load an isolated copy; don't poison imports of other app modules.
    path = Path(__file__).resolve().parents[1] / "wb_serp" / "ui.py"
    spec = importlib.util.spec_from_file_location("_dashboard_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, mock


def test_dashboard_missing_db_shows_explicit_error(tmp_path, monkeypatch):
    app, st = load_dashboard(monkeypatch)
    cfg = config.Settings.from_env()
    from dataclasses import replace
    monkeypatch.setattr(app.Settings, "from_env", lambda: replace(cfg, database_path=tmp_path / "missing.db"))
    with pytest.raises(Stopped):
        app.main()
    assert "не инициализирована" in st.messages[0][1]


def test_dashboard_empty_db_shows_no_queries(tmp_path, monkeypatch):
    app, st = load_dashboard(monkeypatch)
    cfg = config.Settings.from_env()
    from dataclasses import replace
    db = tmp_path / "empty.db"
    Store(db).initialize()
    monkeypatch.setattr(app.Settings, "from_env", lambda: replace(cfg, database_path=db))
    with pytest.raises(Stopped):
        app.main()
    assert "нет запросов" in st.messages[0][1]


def test_dashboard_complete_read_only_flow_with_chart_and_two_csv_exports(tmp_path, monkeypatch):
    app, st = load_dashboard(monkeypatch)
    cfg = config.Settings.from_env()
    from dataclasses import replace
    db = tmp_path / "dashboard.sqlite"
    storage = Store(db)
    storage.initialize()
    storage.sync_queries(["постельное бельё", "пастельный комплект 1,5"])
    query = storage.enabled_queries()[0]
    def write(run_id, ids, at):
        storage.start_run(run_id=run_id, query_id=query["id"],
                          scheduled_for="2026-10-16T00:00:00+00:00",
                          started_at=at, requested_top_n=2, destination=cfg.destination)
        storage.complete_run(run_id=run_id,
                             products=[Product(n, index + 1, Decimal("100.99"), 4.8,
                                               12, None, "Тест продавец", f"Тест {n}")
                                       for index, n in enumerate(ids)],
                             finished_at=at, status="success")
    now = datetime.now(timezone.utc)
    write("run1", [11, 12], now - timedelta(hours=2))
    write("run2", [12, 13], now - timedelta(hours=1))
    monkeypatch.setattr(app.Settings, "from_env", lambda: replace(cfg, database_path=db, top_n=2))
    app.main()
    assert len(st.plots) == 1
    chart = st.plots[0]
    assert chart.layout.yaxis.autorange == "reversed"
    assert len(chart.data) == 2
    assert len(st.exports) == 2
    assert all(isinstance(blob, bytes) and len(blob) > 20 for _, blob in st.exports)
    history_csv = next(blob for label, blob in st.exports if label == "Скачать историю CSV")
    positions = [row["position"] for row in csv.DictReader(
        io.StringIO(history_csv.decode("utf-8-sig")))]
    assert set(positions) == {"", "1", "2"}
    assert len(st.frames) >= 3
    assert list(st.frames[0]["Артикул"]) == [12, 13]
    assert (st.frames[0]["Цена, ₽"] == 100.99).all()
    assert len(st.frames[1]) == 2  # monitor includes both configured phrases


def test_partial_snapshot_is_selectable_exported_and_marked(tmp_path, monkeypatch):
    from dataclasses import replace
    app, st = load_dashboard(monkeypatch)
    cfg = replace(config.Settings.from_env(), database_path=tmp_path / "partial.sqlite", top_n=200)
    storage = Store(cfg.database_path)
    storage.initialize()
    storage.sync_queries(["короткая выдача"])
    qid = storage.enabled_queries()[0]["id"]
    now = datetime.now(timezone.utc)
    storage.start_run(run_id="short", query_id=qid, scheduled_for=now.isoformat(),
                      started_at=now, requested_top_n=200, destination=cfg.destination)
    storage.complete_run(run_id="short", status="partial", finished_at=now,
                         products=[Product(77, 1, None, None, None, None, None, "Товар")])
    monkeypatch.setattr(app.Settings, "from_env", lambda: cfg)
    app.main()
    assert st.session_state[f"chosen_products_{qid}"] == [77]
    assert list(st.plots[0].data[0].y) == [1]
    assert list(st.plots[0].data[0].marker.symbol) == ["diamond-open"]
    assert ("warning", "Неполный сбор: 1 из 200 товаров.") in st.messages
    assert list(st.frames[0]["Артикул"]) == [77]
    assert list(st.frames[0]["Статус"]) == ["Неполный"]
    assert len(st.exports) == 2
