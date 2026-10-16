"""Extract the leading date from a photo description.

Descriptions written while scanning start with a date of varying precision:
"1985/06/16 ...", "1982/09 ...", "1979 ...", or no date at all. The result keeps
that precision: "YYYY", "YYYY/MM", or "YYYY/MM/DD" ("" when there is none).
"""

import calendar
import re
from typing import Optional

MIN_YEAR = 1800
MAX_YEAR = 2100

# Must be followed by a non-digit (or the end) so "19851" or "1985/061" are rejected.
_DATE_RE = re.compile(r"^\s*(\d{4})(?:/(\d{1,2})(?:/(\d{1,2}))?)?(?![\d/])")


def normalize_date(value: str) -> Optional[str]:
    """Validate a user-entered date ("YYYY", "YYYY/MM", "YYYY/MM/DD", or "").
    Returns the zero-padded form, or None if it is not a complete valid date."""
    value = (value or "").strip()
    if not value:
        return ""
    m = _DATE_RE.match(value)
    if not m or m.end() != len(value):
        return None
    parsed = parse_description_date(value)
    # The parser degrades bad months/days to a coarser date; reject that here.
    if parsed.count("/") != value.count("/"):
        return None
    return parsed


def parse_description_date(description: str) -> str:
    m = _DATE_RE.match(description or "")
    if not m:
        return ""
    year = int(m.group(1))
    if not MIN_YEAR <= year <= MAX_YEAR:
        return ""
    if m.group(2) is None:
        return f"{year:04d}"
    month = int(m.group(2))
    if not 1 <= month <= 12:
        return f"{year:04d}"
    if m.group(3) is None:
        return f"{year:04d}/{month:02d}"
    day = int(m.group(3))
    if not 1 <= day <= calendar.monthrange(year, month)[1]:
        return f"{year:04d}/{month:02d}"
    return f"{year:04d}/{month:02d}/{day:02d}"
