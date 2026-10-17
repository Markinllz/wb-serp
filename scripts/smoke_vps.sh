#!/usr/bin/env bash
# Check the running platform without creating additional WB requests.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if ! command -v docker >/dev/null 2>&1; then
  echo 'ERROR: Docker CLI is not visible in this shell.' >&2
  exit 10
fi
if ! docker info >/dev/null 2>&1; then
  echo 'ERROR: Docker daemon is not available for this user.' >&2
  exit 11
fi
compose=(docker compose -f compose.yaml -f compose.browser.yaml)
printf '\n[1/5] Validate Compose syntax...\n'
"${compose[@]}" config --quiet
printf '\n[2/5] List services...\n'
"${compose[@]}" ps
printf '\n[3/5] Check Streamlit health...\n'
curl --fail --silent --show-error --retry 3 --retry-connrefused --retry-delay 2 \
  --max-time 10 http://127.0.0.1:8501/_stcore/health
printf '\n[4/5] Check SQLite integrity and collection history...\n'
"${compose[@]}" exec -T dashboard python - <<'PY'
import sqlite3
from contextlib import closing
from pathlib import Path
p = Path('/app/data/serp.db')
if not p.exists():
    raise SystemExit('FAIL: database does not exist')
with closing(sqlite3.connect(f'file:{p}?mode=ro', uri=True)) as db:
    integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
    count = db.execute('SELECT count(*) FROM search_queries WHERE enabled=1').fetchone()[0]
    statuses = dict(db.execute('SELECT status, count(*) FROM collection_runs GROUP BY status'))
    observations = db.execute('SELECT count(*) FROM observations').fetchone()[0]
    latest = db.execute("SELECT max(completed_at) FROM collection_runs WHERE status='success'").fetchone()[0]
    print('integrity:', integrity, 'enabled queries:', count)
    print('statuses:', statuses, 'observations:', observations, 'last success UTC:', latest)
    canonical = [q for q in Path('/app/config/queries.txt').read_text().splitlines()
                 if q.strip() and not q.lstrip().startswith('#')]
    saved = {q[0] for q in db.execute('SELECT phrase FROM search_queries')}
    if integrity != 'ok' or not set(canonical) <= saved:
        raise SystemExit('FAIL: database consistency or canonical queries missing')
    if not latest:
        raise SystemExit('FAIL: no successful live snapshots yet')
PY
printf '\n[5/5] Check background collector...\n'
if [[ -z "$("${compose[@]}" ps --status running -q collector)" ]]; then
  echo 'Collector is stopped. Dashboard history is available; regular collection is inactive.' >&2
  exit 20
fi
echo 'PASS: dashboard, database and collector are available. No additional WB requests sent.'
