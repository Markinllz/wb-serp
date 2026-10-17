import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize('status', [0, 2])
def test_live_runner_exports_report_and_preserves_exit_code(tmp_path, status):
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    source = Path(__file__).resolve().parents[1] / 'scripts/test_live_full.sh'
    (scripts / source.name).write_text(source.read_text())
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    docker = bindir / 'docker'
    docker.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args=sys.argv[1:]
if args[0] == 'cp':
    Path(args[2]).write_text(json.dumps({'fixture': True})+'\\n')
elif 'run' in args:
    assert 'WB_TOP_N=200' in args
    assert 'WB_DESTINATION=1259570991' in args
    assert any(a.startswith('WB_BROWSER_PROFILE_PATH=/app/data/browser-profile-') for a in args)
    assert args[args.index('-m')+1] == 'scripts.probe_batch_wb'
    assert '--save-live' in args
    print('synthetic CLI control only, no WB requests')
    sys.exit(int(os.environ['FIXTURE_STATUS']))
''')
    docker.chmod(0o755)
    env = {**os.environ, 'PATH': str(bindir) + os.pathsep + os.environ['PATH'],
           'FIXTURE_STATUS': str(status)}
    result = subprocess.run(['bash', str(scripts / source.name), '--skip-build', '--label', 'fixture'],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == status, result.stdout + result.stderr
    reports = list((tmp_path / 'data/live-tests').iterdir())
    assert len(reports) == 1
    assert (reports[0] / 'summary.json').exists()
    assert (reports[0] / 'results.jsonl').exists()
