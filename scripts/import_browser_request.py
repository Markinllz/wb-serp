"""Read Copy as cURL from stdin; store session privately and print nonsecret config."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import tempfile

from wb_serp.browser_session import parse_curl
from wb_serp.config import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stdin', action='store_true', required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        print('Waiting for Copy as cURL input. Paste it, press Enter, then Ctrl+D.', file=sys.stderr)
        session = parse_curl(sys.stdin.read(1_000_001))
        cfg = Settings.from_env()
        output = args.output or cfg.database_path.parent / 'search-session.json'
        output.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix='.search-session-', dir=output.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(asdict(session), stream, ensure_ascii=False)
            os.replace(temp_name, output)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
    except ValueError as exc:
        print(f'Import failed: {exc}', file=sys.stderr)
        return 2
    print(f'WB_SEARCH_URL={session.endpoint}')
    print(f'WB_DESTINATION={session.destination}')
    print('WB_HTTP_TRANSPORT=curl_cffi')
    print(f'WB_SESSION_FILE={output}')
    print('Session saved with mode 0600. Cookies are not printed. '
          'Live access is not yet verified.', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
