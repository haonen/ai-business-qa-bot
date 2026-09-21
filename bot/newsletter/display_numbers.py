"""Presentation-only rounding; calculations and JSON keep full precision."""
import re
from decimal import Decimal, ROUND_HALF_UP

def gmv(value):
    if value is None:return '—'
    return f'{Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP):,}'

def brand_gmv(value):
    if value is None:return "—"
    return f'{Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP):,.1f}'

def round_gmv_text(text):
    return re.sub(r'(?<![\w.])([+-]?[\d,]+\.\d+)(?=\s*M\b)',
                  lambda m:gmv(m[1].replace(',','')),str(text))
