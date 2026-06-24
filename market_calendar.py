"""Small dependency-free U.S. equity calendar for automation gates."""

from __future__ import annotations

from datetime import date, timedelta

from pandas.tseries.holiday import (
    AbstractHolidayCalendar, GoodFriday, Holiday, MO, TH, nearest_workday,
)
from pandas.tseries.offsets import DateOffset, Day
import pandas as pd


class USMarketHolidayCalendar(AbstractHolidayCalendar):
    rules = [
        Holiday("New Year", month=1, day=1, observance=nearest_workday),
        Holiday("Martin Luther King Jr.", month=1, day=1, offset=DateOffset(weekday=MO(3))),
        Holiday("Washington's Birthday", month=2, day=1, offset=DateOffset(weekday=MO(3))),
        GoodFriday,
        Holiday("Memorial Day", month=5, day=31, offset=DateOffset(weekday=MO(-1))),
        Holiday("Juneteenth", month=6, day=19, start_date="2022-01-01", observance=nearest_workday),
        Holiday("Independence Day", month=7, day=4, observance=nearest_workday),
        Holiday("Labor Day", month=9, day=1, offset=DateOffset(weekday=MO(1))),
        Holiday("Thanksgiving", month=11, day=1, offset=DateOffset(weekday=TH(4))),
        Holiday("Christmas", month=12, day=25, observance=nearest_workday),
    ]


def market_holidays(start: date, end: date) -> set:
    holidays = USMarketHolidayCalendar().holidays(start=start, end=end)
    return {timestamp.date() for timestamp in holidays}


def is_market_session(value: date) -> bool:
    return value.weekday() < 5 and value not in market_holidays(value - timedelta(days=7), value + timedelta(days=7))


def market_sessions(start: date, end: date) -> list:
    if end < start:
        return []
    return [timestamp.date() for timestamp in pd.date_range(start, end, freq="D") if is_market_session(timestamp.date())]


def sessions_until(start: date, end: date) -> int:
    """Count sessions after start through end; negative when end precedes start."""
    if end >= start:
        return len([session for session in market_sessions(start, end) if session > start])
    return -len([session for session in market_sessions(end, start) if session > end])
