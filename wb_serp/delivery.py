"""Calendar delivery from the WB frontend's own display function.

The browser adapter obtains the localized text using getDeliveryDisplay, the
same export used by product snippets. Raw time1/time2 alone are not day counts.
Unknown display formats stay NULL; historical observations are never backfilled.
"""
from datetime import date, timedelta
import re

MONTHS = {'января': 1, 'янв': 1, 'февраля': 2, 'фев': 2, 'марта': 3, 'мар': 3,
          'апреля': 4, 'апр': 4, 'мая': 5, 'май': 5, 'июня': 6, 'июн': 6,
          'июля': 7, 'июл': 7, 'августа': 8, 'авг': 8, 'сентября': 9, 'сен': 9,
          'октября': 10, 'окт': 10, 'ноября': 11, 'ноя': 11, 'декабря': 12, 'дек': 12}


def calendar_date(label: str | None, today: date) -> str | None:
    if not isinstance(label, str):
        return None
    label = label.strip().lower().replace('\u00a0', ' ')
    for word, days in [('послезавтра', 2), ('завтра', 1), ('сегодня', 0)]:
        if label == word or label.startswith(word + ' ' ) or label.startswith(word + ','):
            return (today + timedelta(days=days)).isoformat()
    if re.fullmatch(r'(?:за )?(?:час|\d+(?:[–—-]\d+)?\s*час(?:а|ов)?)', label):
        return today.isoformat()
    match = re.fullmatch(r'(\d{1,2})\s+([а-я]+)\.?', label)
    if not match or match[2] not in MONTHS:
        return None
    try:
        result = date(today.year, MONTHS[match[2]], int(match[1]))
        if result < today:
            # Search snippets omit the year; a December delivery may be in January.
            if today.month != 12 or result.month != 1:
                return None
            result = result.replace(year=today.year + 1)
        return result.isoformat()
    except ValueError:
        return None
