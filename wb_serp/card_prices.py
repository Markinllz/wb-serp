"""Read amounts printed on WB cards; no discount formulas or estimated prices."""
from decimal import Decimal, InvalidOperation
import re


def displayed_kopecks(text: str) -> int | None:
    if not isinstance(text, str):
        return None
    match = re.fullmatch(r'\s*(\d[\d \u00a0\u202f]*(?:[.,]\d{1,2})?)\s*₽\s*', text)
    if not match:
        return None
    number = re.sub(r'[ \u00a0\u202f]', '', match[1]).replace(',', '.')
    try:
        amount = Decimal(number) * 100
        return int(amount) if amount.is_finite() and 0 <= amount < 2**63 else None
    except (InvalidOperation, ValueError):
        return None
