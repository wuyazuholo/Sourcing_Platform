"""仅提取用户明确给出的 HS 与时间范围；不猜测商品翻译和市场。"""

import re
from datetime import date

HS = re.compile(r"(?<![A-Za-z0-9])HS\s*[:：]?\s*([0-9]{2,12})(?![0-9])", re.IGNORECASE)
NEGATION = re.compile(r"不要|不是|排除|取消|别用|不使用|不选|非|exclude|not\b", re.IGNORECASE)
YEAR_PERIOD = re.compile(r"(?<![0-9])((?:19|20)[0-9]{2})\s*年?(?:\s*全年|\s*全年度)?(?![0-9-])")
DATE_RANGE = re.compile(
    r"(?<![0-9])(\d{4}-\d{2}-\d{2})\s*(?:至|到|~|—|–|->|to)\s*(\d{4}-\d{2}-\d{2})(?![0-9])",
    re.IGNORECASE,
)


def explicit_hs(text: str) -> str | None:
    matches = list(HS.finditer(text))
    if len(matches) != 1:
        return None
    match = matches[0]
    if NEGATION.search(text[max(0, match.start() - 12) : match.start()]):
        return None
    return match.group(1)


def explicit_period(text: str) -> tuple[date, date] | None:
    years, ranges = list(YEAR_PERIOD.finditer(text)), list(DATE_RANGE.finditer(text))
    if len(years) + len(ranges) != 1:
        return None
    match = (years or ranges)[0]
    if NEGATION.search(text[max(0, match.start() - 12) : match.start()]):
        return None
    try:
        if years:
            year = int(match.group(1))
            return date(year, 1, 1), date(year, 12, 31)
        start, end = date.fromisoformat(match.group(1)), date.fromisoformat(match.group(2))
        return (start, end) if start <= end else None
    except ValueError:
        return None
