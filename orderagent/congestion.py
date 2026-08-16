"""Time-pattern congestion estimation (FR-3).

There is no realtime queue data in any official API, so this is an estimate
from weekday/time patterns and says so explicitly -- the caller must keep the
word 推定 in anything spoken to the user, and the log records that the advice
was pattern-based, per the requirement doc's 制約の明示.
"""
from __future__ import annotations

import datetime as _dt

# (start_minute, end_minute, level) per day class. Levels: busy > mild > calm.
_WEEKDAY = [(11 * 60 + 30, 13 * 60 + 30, "busy"),   # lunch
            (18 * 60, 19 * 60 + 30, "mild")]         # dinner
_WEEKEND = [(11 * 60, 13 * 60 + 30, "busy"),
            (17 * 60 + 30, 19 * 60 + 30, "busy"),
            (10 * 60, 11 * 60, "mild"),
            (14 * 60, 17 * 60, "mild")]


def estimate(when: _dt.datetime | None = None) -> dict:
    """Returns {"level": "busy"|"mild"|"calm", "advice": str, "basis": str}."""
    when = when or _dt.datetime.now()
    minute = when.hour * 60 + when.minute
    windows = _WEEKEND if when.weekday() >= 5 else _WEEKDAY
    level = "calm"
    for start, end, w_level in windows:
        if start <= minute < end:
            level = w_level
            break
    advice = {
        "busy": "この時間帯は混雑していると推定されます。ドライブスルーより店舗受け取りの方が早いかもしれません。",
        "mild": "この時間帯はやや混んでいると推定されます。",
        "calm": "この時間帯は空いていると推定されます。",
    }[level]
    return {"level": level, "advice": advice,
            "basis": "time_pattern_estimate"}  # never realtime data; logged as such
