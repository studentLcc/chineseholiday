"""Shared memorial-day configuration for the integration and card."""
from __future__ import annotations

import datetime
import re

_SOLAR_FULL = re.compile(
    r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?$"
)
_SOLAR_COMPACT = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_SOLAR_MONTH = re.compile(r"^(\d{1,2})-(\d{1,2})$")
_SOLAR_MONTH_COMPACT = re.compile(r"^(\d{2})(\d{2})$")
_LUNAR = re.compile(r"^(?:(\d{4})-)?L(L?)(\d{1,2})-(\d{1,2})$", re.IGNORECASE)


def export_memorials(solar: dict, lunar: dict, timers: list) -> list[dict]:
    """Return the card-facing list stored in the integration."""
    items: list[dict] = []
    items.extend(_export_map(solar, "solar"))
    items.extend(_export_map(lunar, "lunar"))
    for item in timers or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        date = str(item.get("date") or "").strip()
        if name and date:
            items.append({
                "name": name,
                "calendar": "solar",
                "date": date,
                "mode": "timer",
                "key": date,
            })
    return items


def parse_lunar_key(key: str) -> tuple[int | None, int, int, bool]:
    """Return year, month, day, and whether the lunar month is a leap month."""
    raw = str(key).strip()
    leap = raw.startswith("L")
    digits = raw[1:] if leap else raw
    if len(digits) == 8 and digits.isdigit():
        return int(digits[:4]), int(digits[4:6]), int(digits[6:8]), leap
    if len(digits) == 4 and digits.isdigit():
        return None, int(digits[:2]), int(digits[2:4]), leap
    raise ValueError(f"无效农历纪念日：{key}")


def apply_memorial(
    solar: dict,
    lunar: dict,
    timers: list,
    *,
    name: str,
    calendar: str,
    date: str,
    mode: str = "yearly_count",
    operation: str = "add",
) -> dict:
    """Add or delete one memorial in the integration configuration."""
    stored = normalize_memorial(name, calendar, date, mode)
    if stored["store"] == "timer":
        target = timers
    elif stored["store"] == "solar":
        target = solar
    else:
        target = lunar
    if operation == "delete":
        _delete(target, stored)
    elif operation == "add":
        _add(target, stored)
    else:
        raise ValueError("operation 只能是 add 或 delete")
    return stored


def normalize_memorial(name: str, calendar: str, date: str, mode: str) -> dict:
    """Convert a card memorial into the integration storage shape."""
    cleaned_name = str(name or "").strip()
    if not cleaned_name:
        raise ValueError("纪念日名称不能为空")
    calendar_name = "lunar" if str(calendar).lower() == "lunar" else "solar"
    selected_mode = mode if mode in {"yearly", "yearly_count", "timer"} else "yearly_count"
    parsed = _parse_date(str(date or "").strip(), calendar_name)
    if parsed is None or not 1 <= parsed["month"] <= 12 or not 1 <= parsed["day"] <= 31:
        raise ValueError("日期格式无效")
    if calendar_name == "lunar" and (selected_mode == "timer" or parsed["hour"] is not None):
        raise ValueError("农历纪念日按每年重复保存，不能使用精确计时")

    if selected_mode == "timer":
        if parsed["year"] is None:
            raise ValueError("精确计时需要完整的公历日期和时间")
        stamp = datetime.datetime(
            parsed["year"], parsed["month"], parsed["day"],
            parsed["hour"] or 0, parsed["minute"] or 0, parsed["second"] or 0,
        )
        value = stamp.strftime("%Y-%m-%d %H:%M:%S")
        return {
            "name": cleaned_name,
            "store": "timer",
            "key": value,
            "calendar": "solar",
            "date": value,
            "mode": "timer",
        }

    key = f"{parsed['month']:02d}{parsed['day']:02d}"
    if selected_mode == "yearly_count" and parsed["year"] is not None:
        key = f"{parsed['year']:04d}{key}"
    if calendar_name == "lunar" and parsed["leap"]:
        key = f"L{key}"
    return {
        "name": cleaned_name,
        "store": calendar_name,
        "key": key,
        "calendar": calendar_name,
        "date": _display_date(calendar_name, key),
        "mode": "yearly_count" if len(key.lstrip("L")) == 8 else "yearly",
    }


def _parse_date(value: str, calendar: str) -> dict | None:
    lunar = _LUNAR.fullmatch(value)
    if lunar:
        if calendar != "lunar":
            return None
        return {
            "year": int(lunar.group(1)) if lunar.group(1) else None,
            "leap": bool(lunar.group(2)),
            "month": int(lunar.group(3)),
            "day": int(lunar.group(4)),
            "hour": None,
            "minute": None,
            "second": None,
        }
    for pattern in (_SOLAR_FULL, _SOLAR_COMPACT):
        matched = pattern.fullmatch(value)
        if not matched:
            continue
        groups = matched.groups()
        return {
            "year": int(groups[0]),
            "leap": False,
            "month": int(groups[1]),
            "day": int(groups[2]),
            "hour": int(groups[3]) if len(groups) > 3 and groups[3] else None,
            "minute": int(groups[4]) if len(groups) > 4 and groups[4] else None,
            "second": int(groups[5]) if len(groups) > 5 and groups[5] else 0,
        }
    patterns = (_SOLAR_MONTH, _SOLAR_MONTH_COMPACT) if calendar == "solar" else (_SOLAR_MONTH_COMPACT,)
    for pattern in patterns:
        matched = pattern.fullmatch(value)
        if matched:
            return {
                "year": None,
                "leap": False,
                "month": int(matched.group(1)),
                "day": int(matched.group(2)),
                "hour": None,
                "minute": None,
                "second": None,
            }
    return None


def _display_date(calendar: str, key: str) -> str:
    leap = key.startswith("L")
    digits = key[1:] if leap else key
    if calendar == "lunar":
        prefix = "LL" if leap else "L"
        if len(digits) == 8:
            return f"{digits[:4]}-{prefix}{int(digits[4:6])}-{int(digits[6:8])}"
        return f"{prefix}{int(digits[:2])}-{int(digits[2:])}"
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return f"{digits[:2]}-{digits[2:]}"


def _export_map(values: dict, calendar: str) -> list[dict]:
    exported = []
    for key, names in (values or {}).items():
        if isinstance(names, str):
            names = [names]
        if not isinstance(names, list):
            continue
        try:
            date = _display_date(calendar, str(key))
        except (TypeError, ValueError):
            continue
        mode = "yearly_count" if len(str(key).lstrip("L")) == 8 else "yearly"
        for name in names:
            cleaned = str(name).strip()
            if cleaned:
                exported.append({
                    "name": cleaned,
                    "calendar": calendar,
                    "date": date,
                    "mode": mode,
                    "key": str(key),
                })
    return exported


def _add(target, stored: dict) -> None:
    if stored["store"] == "timer":
        target[:] = [
            item for item in target
            if not (
                isinstance(item, dict)
                and item.get("name") == stored["name"]
                and item.get("date") == stored["key"]
            )
        ]
        target.append({"name": stored["name"], "date": stored["key"]})
        return
    names = list(target.get(stored["key"]) or [])
    if stored["name"] not in names:
        names.append(stored["name"])
    target[stored["key"]] = names


def _delete(target, stored: dict) -> None:
    if stored["store"] == "timer":
        target[:] = [
            item for item in target
            if not (
                isinstance(item, dict)
                and item.get("name") == stored["name"]
                and str(item.get("date")) == stored["key"]
            )
        ]
        return
    names = [
        name for name in list(target.get(stored["key"]) or [])
        if str(name).strip() != stored["name"]
    ]
    if names:
        target[stored["key"]] = names
    else:
        target.pop(stored["key"], None)
