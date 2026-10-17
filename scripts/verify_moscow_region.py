"""Verify the configured WB destination for a fixed public point in Moscow."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx

from wb_serp.config import Settings

ENDPOINT = 'https://user-geo-data.wildberries.ru/get-geo-info'
MOSCOW = {'latitude': '55.7539', 'longitude': '37.6208',
          'address': 'Москва, Красная площадь', 'currency': 'RUB'}


def verify(client: httpx.Client, destination: int) -> dict:
    response = client.get(ENDPOINT, params=MOSCOW,
                          headers={'Accept': 'application/json', 'Referer': 'https://www.wildberries.ru/'})
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get('xinfo'), str):
        raise ValueError('Geo response has no xinfo')
    values = parse_qs(payload['xinfo']).get('dest', [])
    if len(values) != 1:
        raise ValueError('Geo response has no unique destination')
    actual = int(values[0])
    # WB also returns IP and a signed userDataSign. Never log or persist them.
    return {'checked_at': datetime.now(timezone.utc).isoformat(),
            'status': 'PASS' if actual == destination else 'MISMATCH',
            'endpoint': ENDPOINT, 'request': MOSCOW, 'http_status': response.status_code,
            'configured_destination': destination, 'moscow_destination': actual}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        with httpx.Client(timeout=15) as client:
            report = verify(client, Settings.from_env().destination)
    except (httpx.HTTPError, ValueError) as exc:
        print(json.dumps({'status': 'UNVERIFIED', 'error_type': type(exc).__name__}))
        return 2
    text = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as output:
            output.write(text)
    print(text, end='')
    return 0 if report['status'] == 'PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
