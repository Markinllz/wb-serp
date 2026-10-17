"""SQLite persistence and read models.

One collector writes observations; the web UI can change query settings.
A run is never overwritten. Partial snapshots retain observed ranks; failed runs
are diagnostic only. Missing ranks in partial snapshots do not prove absence.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Iterator, Sequence

from wb_serp.models import Product, RunStatus

SCHEMA_VERSION = 4


def iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("An aware datetime is required")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


class Store:
    def __init__(self, path: Path, *, readonly: bool = False) -> None:
        self.path = path
        self.readonly = readonly

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        if self.readonly:
            db = sqlite3.connect(f"file:{self.path.absolute()}?mode=ro", uri=True, timeout=30)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout = 30000")
        db.execute("PRAGMA foreign_keys = ON")
        # synchronous is per CONNECTION, unlike persistent journal_mode=WAL.
        # FULL favors history durability after a sudden VPS power loss;
        # there are only ~70 snapshot writes per hour on this workload.
        if not self.readonly:
            db.execute("PRAGMA synchronous = FULL")
        try:
            yield db
            if not self.readonly:
                db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self) -> None:
        if self.readonly:
            raise ValueError("Read-only store cannot initialize schema")
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError("DB is newer than this code; do not downgrade")
            if version == SCHEMA_VERSION:
                self._assert_schema(db)
                return
            if version not in (0, 1, 2, 3):
                raise RuntimeError(f"Unsupported schema migration from {version}")
            if version == 0:
                db.executescript("""
                CREATE TABLE IF NOT EXISTS search_queries (
                    id INTEGER PRIMARY KEY,
                    phrase TEXT NOT NULL UNIQUE,
                    display_order INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1))
                );
                CREATE TABLE IF NOT EXISTS collection_runs (
                    id TEXT PRIMARY KEY,
                    query_id INTEGER NOT NULL REFERENCES search_queries(id),
                    scheduled_for TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    requested_top_n INTEGER NOT NULL CHECK(requested_top_n > 0),
                    destination INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN
                        ('running', 'success', 'partial', 'failed', 'blocked', 'interrupted')),
                    observed_count INTEGER NOT NULL DEFAULT 0 CHECK(observed_count >= 0),
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS runs_by_query
                    ON collection_runs(query_id, started_at DESC);
                CREATE INDEX IF NOT EXISTS runs_by_status
                    ON collection_runs(status, started_at DESC);
                CREATE INDEX IF NOT EXISTS runs_by_slot
                    ON collection_runs(query_id, scheduled_for, status);
                CREATE TABLE IF NOT EXISTS observations (
                    run_id TEXT NOT NULL REFERENCES collection_runs(id),
                    position INTEGER NOT NULL CHECK(position > 0),
                    nm_id INTEGER NOT NULL CHECK(nm_id > 0),
                    price_kopecks INTEGER CHECK(price_kopecks >= 0),
                    rating REAL CHECK(rating >= 0 AND rating <= 5),
                    reviews INTEGER CHECK(reviews >= 0),
                    delivery_days INTEGER CHECK(delivery_days >= 0),
                    seller TEXT,
                    title TEXT,
                    PRIMARY KEY (run_id, position),
                    UNIQUE (run_id, nm_id)
                );
                CREATE INDEX IF NOT EXISTS observations_by_product
                    ON observations(nm_id, run_id);
                CREATE TABLE IF NOT EXISTS product_catalog (
                    query_id INTEGER NOT NULL REFERENCES search_queries(id),
                    nm_id INTEGER NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    last_title TEXT,
                    last_seller TEXT,
                    PRIMARY KEY (query_id, nm_id)
                );
                CREATE INDEX IF NOT EXISTS catalog_latest
                    ON product_catalog(query_id, last_seen DESC);
                """)
            if version < 2:
                db.execute("""CREATE TABLE IF NOT EXISTS dataset_settings (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                )""")
            if version < 4:
                db.execute('BEGIN IMMEDIATE')
                columns = {r['name'] for r in db.execute('PRAGMA table_info(search_queries)')}
                if columns and 'enabled_since' not in columns:
                    db.execute('''ALTER TABLE search_queries ADD COLUMN enabled_since REAL
                        NOT NULL DEFAULT 0 CHECK(enabled_since>=0)''')
                if columns:
                    db.execute('UPDATE search_queries SET enabled_since=? WHERE enabled_since=0',
                               (datetime.now(timezone.utc).timestamp(),))
            self._assert_schema(db)
            if version < 3:
                # Version 2 omitted valid short catalogs from product selection.
                # Backfill the index only; never alter observations or run status.
                db.execute("""INSERT INTO product_catalog
                    (query_id, nm_id, first_seen, last_seen, last_title, last_seller)
                    SELECT query_id, nm_id, first_seen, started_at, title, seller
                    FROM (
                        SELECT r.query_id, o.nm_id, r.started_at, o.title, o.seller,
                            min(r.started_at) OVER (
                                PARTITION BY r.query_id, o.nm_id) AS first_seen,
                            row_number() OVER (PARTITION BY r.query_id, o.nm_id
                                ORDER BY r.started_at DESC, r.rowid DESC) AS latest
                        FROM observations o JOIN collection_runs r ON r.id=o.run_id
                        WHERE r.status='partial'
                    ) WHERE latest=1
                    ON CONFLICT(query_id, nm_id) DO UPDATE SET
                        first_seen=min(product_catalog.first_seen, excluded.first_seen),
                        last_seen=max(product_catalog.last_seen, excluded.last_seen),
                        last_title=CASE WHEN excluded.last_seen >= product_catalog.last_seen
                            THEN COALESCE(excluded.last_title, product_catalog.last_title)
                            ELSE product_catalog.last_title END,
                        last_seller=CASE WHEN excluded.last_seen >= product_catalog.last_seen
                            THEN COALESCE(excluded.last_seller, product_catalog.last_seller)
                            ELSE product_catalog.last_seller END""")
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def _assert_schema(db: sqlite3.Connection) -> None:
        """Fail loudly on partially migrated or manually damaged databases."""
        required = {
            "search_queries": {"id", "phrase", "enabled", "display_order", "enabled_since"},
            "collection_runs": {"id", "query_id", "scheduled_for", "status", "requested_top_n", "destination"},
            "observations": {"run_id", "nm_id", "position", "price_kopecks"},
            "product_catalog": {"query_id", "nm_id", "first_seen", "last_seen"},
            "dataset_settings": {"key", "value"},
        }
        for table, columns in required.items():
            existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
            if not columns <= existing:
                raise RuntimeError(
                    f"Database schema is incomplete: {table} is missing {sorted(columns - existing)}. "
                    "Restore a backup; do not run automatic destructive repairs."
                )
        required_indexes = {
            "runs_by_query", "runs_by_status", "runs_by_slot",
            "observations_by_product", "catalog_latest",
        }
        existing_indexes = {row["name"] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )}
        if not required_indexes <= existing_indexes:
            raise RuntimeError(
                "Database indexes are incomplete: "
                f"{sorted(required_indexes - existing_indexes)}. Restore schema from a trusted backup."
            )

    def sync_queries(self, phrases: Sequence[str], *, preserve_choices: bool = False) -> None:
        if len(set(phrases)) != len(phrases) or not phrases:
            raise ValueError("Queries must be non-empty and unique")
        with self.connect() as db:
            if not preserve_choices:
                db.execute("UPDATE search_queries SET enabled = 0")
            for order, phrase in enumerate(phrases, start=1):
                update = "display_order=excluded.display_order"
                if not preserve_choices:
                    update += ", enabled=1, enabled_since=excluded.enabled_since"
                db.execute(f"""INSERT INTO search_queries(phrase, display_order, enabled, enabled_since)
                    VALUES(?, ?, 1, ?) ON CONFLICT(phrase) DO UPDATE SET {update}""",
                    (phrase, order, datetime.now(timezone.utc).timestamp()))

    def add_query(self, phrase: str) -> tuple[int, bool]:
        """Add a literal phrase or resume it, without replacing historical IDs."""
        if self.readonly:
            raise ValueError("Read-only store cannot change queries")
        if (not isinstance(phrase, str) or not phrase.strip() or len(phrase) > 300
                or any(ord(c) < 32 or ord(c) == 127 for c in phrase)):
            raise ValueError("Введите запрос длиной от 1 до 300 символов, без переносов строк.")
        if phrase != phrase.strip():
            raise ValueError("Уберите пробелы в начале и конце запроса.")
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT id, enabled FROM search_queries WHERE phrase=?', (phrase,)).fetchone()
            if existing:
                if existing['enabled']:
                    raise ValueError("Этот запрос уже собирается.")
                db.execute('UPDATE search_queries SET enabled=1,enabled_since=? WHERE id=?',
                           (datetime.now(timezone.utc).timestamp(), existing['id']))
                return existing['id'], False
            result = db.execute('''INSERT INTO search_queries (phrase,display_order,enabled,enabled_since)
                VALUES (?,(SELECT COALESCE(max(display_order),0)+1 FROM search_queries),1,?)''',
                (phrase, datetime.now(timezone.utc).timestamp()))
            return result.lastrowid, True

    def set_query_enabled(self, query_id: int, enabled: bool) -> None:
        """Pause/resume collection; never delete queries, runs or observations."""
        if self.readonly:
            raise ValueError("Read-only store cannot change queries")
        if not isinstance(enabled, bool):
            raise ValueError("Enabled must be a boolean")
        with self.connect() as db:
            result = db.execute('''UPDATE search_queries SET enabled=?,
                enabled_since=CASE WHEN enabled=0 AND ?=1 THEN ? ELSE enabled_since END
                WHERE id=?''', (int(enabled), int(enabled), datetime.now(timezone.utc).timestamp(), query_id))
            if not result.rowcount:
                raise ValueError("Запрос не найден. Обновите список.")

    def enabled_queries(self) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute("SELECT id, phrase FROM search_queries WHERE enabled=1 ORDER BY display_order").fetchall()

    def has_completed_slot(self, query_id: int, scheduled_for: str,
                           requested_top_n: int | None = None) -> bool:
        """Deduplicate by slot AND depth; a top-100 run cannot satisfy top-200."""
        with self.connect() as db:
            return bool(db.execute("""SELECT EXISTS(SELECT 1 FROM collection_runs
                WHERE query_id=? AND scheduled_for=? AND status='success'
                  AND (? IS NULL OR requested_top_n=?))""",
                (query_id, scheduled_for, requested_top_n, requested_top_n)).fetchone()[0])

    def interrupt_abandoned_runs(self) -> int:
        with self.connect() as db:
            result = db.execute("""UPDATE collection_runs SET
                 status='interrupted', completed_at=?, error='Worker stopped before completion'
                 WHERE status='running'""", (iso(datetime.now(timezone.utc)),))
            return result.rowcount

    def start_run(
        self, *, run_id: str, query_id: int, scheduled_for: str,
        started_at: datetime, requested_top_n: int, destination: int,
    ) -> None:
        with self.connect() as db:
            fixed_destination = db.execute(
                "SELECT value FROM dataset_settings WHERE key='destination'"
            ).fetchone()
            if fixed_destination is None:
                db.execute("INSERT INTO dataset_settings(key, value) VALUES ('destination', ?)",
                           (str(destination),))
            elif int(fixed_destination["value"]) != destination:
                # Early configuration mistakes must remain repairable if the
                # source has NEVER yielded a usable snapshot. Failed
                # attempts still retain their original per-run destination.
                confirmed = db.execute(
                    "SELECT EXISTS(SELECT 1 FROM collection_runs "
                    "WHERE status IN ('success', 'partial') AND observed_count>0)"
                ).fetchone()[0]
                if confirmed:
                    raise ValueError(
                        "WB_DESTINATION was changed for this dataset. "
                        "Keep the original destination to preserve rank comparability."
                    )
                db.execute("UPDATE dataset_settings SET value=? WHERE key='destination'",
                           (str(destination),))
            db.execute("""INSERT INTO collection_runs
                (id, query_id, scheduled_for, started_at, requested_top_n, destination, status)
                VALUES (?, ?, ?, ?, ?, ?, 'running')""",
                (run_id, query_id, scheduled_for, iso(started_at), requested_top_n, destination))

    def complete_run(
        self, *, run_id: str, products: Sequence[Product], status: RunStatus,
        finished_at: datetime, error: str | None = None,
    ) -> None:
        if status == "running":
            raise ValueError("Cannot complete with running status")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT query_id, started_at, requested_top_n, status FROM collection_runs WHERE id=?",
                             (run_id,)).fetchone()
            if run is None or run["status"] != "running":
                raise ValueError("Run is missing or is already complete")
            if status == "success" and len(products) != run["requested_top_n"]:
                raise ValueError("A successful run must have exactly top_n rows")
            positions = [p.position for p in products]
            nm_ids = [p.nm_id for p in products]
            if (positions != list(range(1, len(products) + 1))
                    or len(nm_ids) != len(set(nm_ids))):
                raise ValueError("Noncontiguous positions or duplicate nm_id")
            db.executemany("""INSERT INTO observations
                (run_id, position, nm_id, price_kopecks, rating, reviews,
                 delivery_days, seller, title) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", [
                (run_id, p.position, p.nm_id,
                 int(p.price_rub * 100) if p.price_rub is not None else None,
                 p.rating, p.reviews, p.delivery_days, p.seller, p.title)
                for p in products
            ])
            db.execute("""UPDATE collection_runs
                SET status=?, completed_at=?, observed_count=?, error=? WHERE id=?""",
                (status, iso(finished_at), len(products), error, run_id))
            if status in ("success", "partial"):
                db.executemany("""INSERT INTO product_catalog
                    (query_id, nm_id, first_seen, last_seen, last_title, last_seller)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(query_id, nm_id) DO UPDATE SET
                        last_seen=excluded.last_seen,
                        last_title=COALESCE(excluded.last_title, product_catalog.last_title),
                        last_seller=COALESCE(excluded.last_seller, product_catalog.last_seller)""", [
                    (run["query_id"], p.nm_id, run["started_at"], run["started_at"], p.title, p.seller)
                    for p in products
                ])

    def queries_dashboard(self, *, include_paused: bool = False) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("""SELECT q.id, q.phrase, q.display_order, q.enabled, q.enabled_since,
                (SELECT status FROM collection_runs r WHERE r.query_id=q.id
                 ORDER BY started_at DESC, rowid DESC LIMIT 1) AS last_status,
                (SELECT started_at FROM collection_runs r WHERE r.query_id=q.id
                 ORDER BY started_at DESC, rowid DESC LIMIT 1) AS last_attempt,
                (SELECT started_at FROM collection_runs r WHERE r.query_id=q.id AND r.status='success'
                 ORDER BY started_at DESC, rowid DESC LIMIT 1) AS last_success,
                (SELECT observed_count FROM collection_runs r WHERE r.query_id=q.id
                 ORDER BY started_at DESC, rowid DESC LIMIT 1) AS last_count
                FROM search_queries q WHERE (? OR q.enabled=1) ORDER BY q.display_order,q.id""",
                (include_paused,)).fetchall()
        return [dict(row) for row in rows]

    def collection_stats(self, *, slot: str, depth: int,
                         query_ids: Sequence[int] | None = None) -> dict:
        """Global totals and latest attempt per planned query, not retry counts."""
        with self.connect() as db:
            db.execute('BEGIN')
            totals = dict(db.execute("""SELECT
                (SELECT count(*) FROM observations o JOIN collection_runs r ON r.id=o.run_id
                    WHERE r.status IN ('success','partial')) AS observations,
                (SELECT count(DISTINCT nm_id) FROM product_catalog) AS products,
                (SELECT count(*) FROM collection_runs WHERE status IN ('success','partial')
                    AND observed_count>0) AS snapshots,
                (SELECT count(*) FROM search_queries WHERE enabled=1) AS active_queries,
                (SELECT max(completed_at) FROM collection_runs WHERE status IN ('success','partial')
                    AND observed_count>0) AS last_saved_at""").fetchone())
            if query_ids is None:
                query_ids = [r[0] for r in db.execute('SELECT id FROM search_queries WHERE enabled=1')]
            if query_ids:
                placeholders = ','.join('?' for _ in query_ids)
                rows = db.execute(f"""WITH attempts AS (
                    SELECT r.*, q.phrase,
                        row_number() OVER (PARTITION BY query_id ORDER BY started_at DESC,r.rowid DESC) AS n
                    FROM collection_runs r JOIN search_queries q ON q.id=r.query_id
                    WHERE scheduled_for=? AND requested_top_n=? AND query_id IN ({placeholders})
                ) SELECT status,observed_count,phrase FROM attempts WHERE n=1""",
                    (slot, depth, *query_ids)).fetchall()
            else:
                rows = []
        counts = {state: sum(r['status'] == state for r in rows)
                  for state in ('success', 'partial', 'failed', 'blocked', 'interrupted', 'running')}
        return {**totals, 'slot': slot, 'target': len(query_ids),
                'processed': sum(n for state, n in counts.items() if state != 'running'),
                'counts': counts,
                'cycle_observations': sum(r['observed_count'] for r in rows
                                          if r['status'] in ('success', 'partial')),
                'current_query': next((r['phrase'] for r in rows if r['status'] == 'running'), None)}

    def available_depths(self, query_id: int, *, include_partial: bool = False) -> list[int]:
        """Keep requested depths separate even when displaying short snapshots."""
        statuses = "('success', 'partial')" if include_partial else "('success')"
        with self.connect() as db:
            rows = db.execute(f"""SELECT DISTINCT requested_top_n FROM collection_runs
                WHERE query_id=? AND status IN {statuses} AND observed_count>0
                ORDER BY requested_top_n DESC""", (query_id,)).fetchall()
        return [row[0] for row in rows]

    def known_products(self, query_id: int) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("""SELECT nm_id, last_title, last_seller, last_seen
                FROM product_catalog WHERE query_id=? ORDER BY last_seen DESC, nm_id""",
                (query_id,)).fetchall()
        return [dict(row) for row in rows]

    def product_count(self, query_id: int) -> int:
        with self.connect() as db:
            return int(db.execute(
                "SELECT count(*) FROM product_catalog WHERE query_id=?", (query_id,)
            ).fetchone()[0])

    def search_products(self, query_id: int, text: str = "", *, limit: int = 250) -> list[dict]:
        """Search the entire catalog with literal, Unicode-insensitive word matching."""
        if not 1 <= limit <= 500:
            raise ValueError("Invalid catalog limit")
        term = text.strip()[:100]
        with self.connect() as db:
            if term:
                # SQLite's built-in LIKE only folds ASCII case. Cyrillic titles
                # need Python's Unicode casefold; е/ё are equivalent for search.
                def normalize(value: str) -> str:
                    return value.casefold().replace("ё", "е")
                db.create_function("search_fold", 1, normalize, deterministic=True)
                words = normalize(term).split()
                field = "search_fold(CAST(nm_id AS TEXT)||' '||COALESCE(last_title,'')||' '||COALESCE(last_seller,''))"
                conditions = " AND ".join(f"instr({field}, ?) > 0" for _ in words)
                rows = db.execute(f"""SELECT nm_id, last_title, last_seller, last_seen
                    FROM product_catalog WHERE query_id=? AND {conditions}
                    ORDER BY last_seen DESC, nm_id LIMIT ?""",
                    (query_id, *words, limit)).fetchall()
            else:
                rows = db.execute("""SELECT nm_id, last_title, last_seller, last_seen
                    FROM product_catalog WHERE query_id=?
                    ORDER BY last_seen DESC, nm_id LIMIT ?""", (query_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def products_by_ids(self, query_id: int, nm_ids: Sequence[int]) -> list[dict]:
        if not nm_ids:
            return []
        if len(nm_ids) > 30:
            raise ValueError("Select at most 30 products")
        placeholders = ",".join("?" for _ in nm_ids)
        with self.connect() as db:
            rows = db.execute(f"""SELECT nm_id, last_title, last_seller, last_seen
                FROM product_catalog WHERE query_id=? AND nm_id IN ({placeholders})""",
                (query_id, *nm_ids)).fetchall()
        return [dict(row) for row in rows]

    def latest_observations(self, query_id: int, *, include_partial: bool = False) -> list[dict]:
        statuses = "('success', 'partial')" if include_partial else "('success')"
        with self.connect() as db:
            rows = db.execute(f"""SELECT o.position, o.nm_id, o.price_kopecks, o.rating,
                  o.reviews, o.delivery_days, o.seller, o.title, r.started_at, r.requested_top_n,
                  r.status AS run_status
                FROM observations o JOIN collection_runs r ON r.id=o.run_id
                WHERE r.id=(SELECT id FROM collection_runs
                    WHERE query_id=? AND status IN {statuses} AND observed_count>0
                    ORDER BY started_at DESC, rowid DESC LIMIT 1)
                ORDER BY o.position""", (query_id,)).fetchall()
        return [dict(row) for row in rows]

    def recent_runs(self, query_id: int | None = None, limit: int = 150) -> list[dict]:
        if not 1 <= limit <= 2000:
            raise ValueError("Invalid limit")
        filter_sql = "WHERE r.query_id=?" if query_id is not None else ""
        params = [query_id, limit] if query_id is not None else [limit]
        with self.connect() as db:
            rows = db.execute(f"""SELECT r.id, q.phrase, r.started_at, r.completed_at,
                    r.status, r.observed_count, r.requested_top_n, r.error, r.destination
                FROM collection_runs r JOIN search_queries q ON r.query_id=q.id
                {filter_sql} ORDER BY r.started_at DESC, r.rowid DESC LIMIT ?""", params).fetchall()
        return [dict(row) for row in rows]

    def count_success_runs(self, query_id: int, *, since: str | None = None,
                           until: str | None = None, depth: int | None = None) -> int:
        return self.count_history_runs(query_id, since=since, until=until, depth=depth)

    def count_history_runs(self, query_id: int, *, since: str | None = None,
                           until: str | None = None, depth: int | None = None,
                           include_partial: bool = False) -> int:
        statuses = "('success', 'partial')" if include_partial else "('success')"
        where = ["query_id=?", f"status IN {statuses}", "observed_count>0"]
        params: list = [query_id]
        if since:
            where.append("started_at>=?")
            params.append(since)
        if until:
            where.append("started_at<=?")
            params.append(until)
        if depth is not None:
            where.append("requested_top_n=?")
            params.append(depth)
        with self.connect() as db:
            return int(db.execute(
                f"SELECT count(*) FROM collection_runs WHERE {' AND '.join(where)}", params
            ).fetchone()[0])

    def rank_history(
        self, query_id: int, nm_ids: Sequence[int], *,
        since: str | None = None, until: str | None = None,
        limit_runs: int = 2200,
        depth: int | None = None,
        include_partial: bool = False,
    ) -> list[dict]:
        """Return dense timeline with SQL NULLs for missing products (never fake ranks).

        Full snapshots prove absence; optional partial snapshots only prove the
        ranks actually observed. run_status distinguishes an unknown partial rank
        from absence in a full snapshot. Both break the line, never invent a rank.
        """
        if not nm_ids:
            return []
        if len(nm_ids) > 30:
            raise ValueError("Select at most 30 products")
        if depth is not None and depth < 1:
            raise ValueError("Depth must be positive")
        if not 1 <= limit_runs <= 5000:
            raise ValueError("Invalid run limit")
        statuses = "('success', 'partial')" if include_partial else "('success')"
        predicates = ["query_id=?", f"status IN {statuses}", "observed_count>0"]
        params: list = [query_id]
        if depth is not None:
            predicates.append("requested_top_n=?")
            params.append(depth)
        if since:
            predicates.append("started_at>=?")
            params.append(since)
        if until:
            predicates.append("started_at<=?")
            params.append(until)
        with self.connect() as db:
            runs = db.execute(f"""SELECT id, started_at, requested_top_n, status FROM collection_runs
                WHERE {' AND '.join(predicates)} ORDER BY started_at DESC, rowid DESC LIMIT ?""",
                [*params, limit_runs]).fetchall()
            if not runs:
                return []
            runs = list(reversed(runs))
            placeholders = ",".join("?" for _ in nm_ids)
            ids_placeholders = ",".join("?" for _ in runs)
            found = db.execute(f"""SELECT run_id, nm_id, position, price_kopecks, rating,
                    reviews, delivery_days, seller, title FROM observations
                WHERE run_id IN ({ids_placeholders}) AND nm_id IN ({placeholders})""",
                [*(r["id"] for r in runs), *nm_ids]).fetchall()
        fields = ("position", "price_kopecks", "rating", "reviews", "delivery_days", "seller", "title")
        by_key = {(r["run_id"], r["nm_id"]): {key: r[key] for key in fields} for r in found}
        return [
            {"run_id": r["id"], "started_at": r["started_at"],
             "requested_top_n": r["requested_top_n"], "nm_id": nm_id,
             **by_key.get((r["id"], nm_id), dict.fromkeys(fields)), "run_status": r["status"]}
            for nm_id in nm_ids for r in runs
        ]

    def snapshot_turnover(self, query_id: int, *, depth: int,
                          since: str | None = None, until: str | None = None,
                          limit_runs: int = 2200) -> list[dict]:
        """New products between consecutive complete snapshots of one depth/region."""
        if depth < 1 or not 1 <= limit_runs <= 5000:
            raise ValueError("Invalid snapshot range")
        predicates = ["query_id=?", "status='success'", "requested_top_n=?"]
        params: list = [query_id, depth]
        for clause, value in (("started_at>=?", since), ("started_at<=?", until)):
            if value:
                predicates.append(clause)
                params.append(value)
        with self.connect() as db:
            rows = db.execute(f"""WITH latest AS (
                SELECT id, started_at, destination, rowid AS run_order
                FROM collection_runs WHERE {' AND '.join(predicates)}
                ORDER BY started_at DESC, rowid DESC LIMIT ?
            ), pairs AS (
                SELECT *, lag(id) OVER w AS previous_id,
                    lag(started_at) OVER w AS previous_at,
                    lag(destination) OVER w AS previous_destination
                FROM latest WINDOW w AS (ORDER BY started_at, run_order)
            ) SELECT id AS run_id, started_at, previous_at,
                (SELECT count(*) FROM observations n WHERE n.run_id=p.id AND NOT EXISTS
                    (SELECT 1 FROM observations o WHERE o.run_id=p.previous_id AND o.nm_id=n.nm_id))
                    AS entered
                FROM pairs p WHERE previous_id IS NOT NULL AND destination=previous_destination
                ORDER BY started_at, run_order""", (*params, limit_runs + 1)).fetchall()
        return [{**dict(row), "depth": depth, "retained": depth - row["entered"],
                 "percent": row["entered"] * 100 / depth} for row in rows]

    def compare_latest_snapshots(self, query_id: int, *, depth: int,
                                 since: str | None = None, until: str | None = None) -> dict | None:
        """Compare two complete snapshots of the same query, region and depth.

        Entering/leaving the top is categorical: an absent rank has no numeric delta.
        Partial and failed attempts cannot substantiate movements of the whole SERP.
        """
        if depth < 1:
            raise ValueError("Depth must be positive")
        predicates = ["query_id=?", "status='success'", "requested_top_n=?"]
        params: list = [query_id, depth]
        for clause, value in (("started_at>=?", since), ("started_at<=?", until)):
            if value:
                predicates.append(clause)
                params.append(value)
        with self.connect() as db:
            runs = db.execute(f"""SELECT id, started_at, requested_top_n, destination
                FROM collection_runs WHERE {' AND '.join(predicates)}
                ORDER BY started_at DESC, rowid DESC LIMIT 2""", params).fetchall()
            if len(runs) < 2:
                return None
            after, before = runs
            if before["destination"] != after["destination"]:
                raise ValueError("Cannot compare snapshots from different destinations")
            rows = db.execute("""SELECT run_id, nm_id, position, title FROM observations
                WHERE run_id IN (?, ?)""", (before["id"], after["id"])).fetchall()
        old = {r["nm_id"]: r for r in rows if r["run_id"] == before["id"]}
        new = {r["nm_id"]: r for r in rows if r["run_id"] == after["id"]}
        products = []
        for nm in sorted(old.keys() | new.keys()):
            previous, current = old.get(nm), new.get(nm)
            delta = previous["position"] - current["position"] if previous and current else None
            status = ("entered" if previous is None else "left" if current is None else
                      "up" if delta > 0 else "down" if delta < 0 else "unchanged")
            products.append({"nm_id": nm, "title": (current["title"] if current else None)
                             or (previous["title"] if previous else None),
                             "before": previous["position"] if previous else None,
                             "after": current["position"] if current else None,
                             "delta": delta, "change": status})
        products.sort(key=lambda r: (r["after"] is None, r["after"] or r["before"], r["nm_id"]))
        return {"before": dict(before), "after": dict(after), "products": products}
