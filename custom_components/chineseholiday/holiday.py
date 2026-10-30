#!/usr/local/bin/python3
# coding=utf-8
"""
节假日模块：从 API 获取并缓存节假日状态
数据源：https://tool.bitefu.net/jiari/ (主) / 本地 JSON 缓存 (备)
"""
from __future__ import annotations

import datetime
import copy
from datetime import datetime as datetime_class
from datetime import timedelta
import asyncio
import json
import logging
import os
import re
from homeassistant.util import dt as dt_util

from . import lunar

_LOGGER = logging.getLogger(__name__)

holiday_database_path = os.path.dirname(os.path.realpath(__file__)) + "/data.db"
holiday_status_json_path = (
    os.path.dirname(os.path.realpath(__file__)) + "/holiday.json"
)

# ---------------------------------------------------------------------------
# 轻量 SQLite 数据库（供 getHoliday 旧接口兼容使用）
# ---------------------------------------------------------------------------

class Holiday:
    """节假日查询与缓存。

    主数据源：https://tool.bitefu.net/jiari/
      - 返回格式 {"YYYYMM": {"MMDD": {"type": "0/1/2", "week2": "1-7", ...}}}
      - type 0=工作日 1=休息日 2=法定节假日
    本地缓存：holiday.json（同目录）
    """

    _BITEFU_API = "https://tool.bitefu.net/jiari/"
    _REQUEST_TIMEOUT = 10
    _CACHE_TTL_DAYS = 15  # 超过 N 天重新从服务器拉取

    def __init__(self, cache_path: str | None = None):
        # __init__ 完全不做 I/O，所有磁盘/网络操作延迟到 _update() 线程中执行
        # 原因：async_setup_entry 在事件循环主线程运行，阻塞 I/O 会被 HA 2024+ 检测并报错
        self._holiday_json: dict = {}
        # Do not write mutable data into the installed custom component.  HA can
        # replace that directory during an update.  The caller normally passes a
        # path under the HA configuration directory; the module path remains a compatibility
        # fallback for direct standalone use.
        self._cache_path = cache_path or holiday_status_json_path

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @classmethod
    def day(cls, n: int) -> datetime_class:
        """Return local Home Assistant time plus ``n`` days.

        HA may run outside China, so a hard-coded UTC+8 offset is incorrect.
        ``dt_util.now`` uses the configured HA timezone.
        """
        return (dt_util.now() + timedelta(days=n)).replace(tzinfo=None)

    @classmethod
    def today(cls) -> datetime_class:
        return cls.day(0)

    @classmethod
    def tomorrow(cls) -> datetime_class:
        return cls.day(1)

    # ------------------------------------------------------------------
    # 磁盘缓存读写
    # ------------------------------------------------------------------

    def get_holidays_from_disk(self):
        for path in (self._cache_path, holiday_status_json_path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self._holiday_json = loaded
                    return
            except Exception as e:
                _LOGGER.debug("get_holidays_from_disk(%s): %s", path, e)

    def _write_cache(self, data: dict):
        try:
            cache_dir = os.path.dirname(self._cache_path)
            if cache_dir:
                os.makedirs(cache_dir, exist_ok=True)
            temp_path = f"{self._cache_path}.tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, self._cache_path)
        except Exception as e:
            _LOGGER.error("写入节假日缓存失败: %s", e)

    # ------------------------------------------------------------------
    # 服务器拉取（bitefu.net）
    # ------------------------------------------------------------------

    def _prepare_refresh(self, days: int = 15) -> dict:
        """Load cache metadata without doing network I/O."""
        data: dict = {}
        update_date = "2000-01-01"

        for path in (self._cache_path, holiday_status_json_path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    break
            except Exception:
                continue

        if not isinstance(data, dict):
            _LOGGER.warning("节假日缓存顶层格式错误，按空缓存处理")
            data = {}
        if data and "update_time" in data:
            update_date = data["update_time"]

        today = self.today()
        today_str = today.strftime("%Y-%m-%d")
        refresh_pending = bool(data.get("refresh_pending"))
        try:
            last_update = datetime_class.strptime(str(update_date), "%Y-%m-%d")
        except (TypeError, ValueError):
            _LOGGER.warning("节假日缓存 update_time 无法解析（%s），按过期处理", update_date)
            last_update = datetime_class(2000, 1, 1)
        interval = today - last_update

        if interval.days <= days and days != 0 and not refresh_pending:
            _LOGGER.debug("get_holidays_from_server: 缓存未过期，跳过更新")
            return {
                "data": data if isinstance(data, dict) else {},
                "should_fetch": False,
                "months": [],
                "today": today_str,
                "update_date": update_date,
                "refresh_pending": refresh_pending,
            }

        months = []
        for i in range(6):
            month = today.month + i
            year = today.year
            if month > 12:
                year += 1
                month -= 12
            months.append((year, month))
        return {
            "data": data if isinstance(data, dict) else {},
            "should_fetch": True,
            "months": months,
            "today": today_str,
            "update_date": update_date,
            "refresh_pending": refresh_pending,
        }

    async def async_refresh(self, hass, days: int = 15) -> str:
        """Refresh holiday data with Home Assistant's shared aiohttp session.

        Returns ``cached`` when the local cache is still fresh, ``updated`` when
        at least one month was fetched, or ``failed`` when a fetch was required
        but every request failed. The previous cache is kept on failure.
        """
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        snapshot = await hass.async_add_executor_job(self._prepare_refresh, days)
        cached = snapshot["data"] if isinstance(snapshot["data"], dict) else {}
        self._holiday_json = cached
        if not snapshot["should_fetch"]:
            return "cached"

        session = async_get_clientsession(hass)
        new_data = copy.deepcopy(cached)
        new_data.setdefault("update_time", snapshot["update_date"])
        attempted = 0
        succeeded = 0
        for year, month in snapshot["months"]:
            attempted += 1
            try:
                year_dict = new_data.setdefault(str(year), {})
                ok = await self._async_fetch_one_month(session, year, month, year_dict)
            except Exception as err:
                ok = False
                _LOGGER.warning("async_refresh: %d-%02d 拉取失败: %s", year, month, err)
            if ok:
                succeeded += 1
        complete = succeeded == attempted and attempted > 0
        if complete:
            new_data["update_time"] = snapshot["today"]
            new_data.pop("refresh_pending", None)
        else:
            new_data["refresh_pending"] = True
            _LOGGER.warning(
                "async_refresh: %s/%s 个月拉取成功，保留待重试标记且不更新缓存时间",
                succeeded,
                attempted,
            )
        self._holiday_json = new_data
        await hass.async_add_executor_job(self._write_cache, new_data)
        if succeeded == 0:
            return "failed"
        return "updated" if complete else "partial"

    async def _async_fetch_one_month(self, session, year: int, month: int, year_dict: dict) -> bool:
        """Fetch one month with the shared async client session."""
        d = f"{year}{month:02d}"
        try:
            async with asyncio.timeout(self._REQUEST_TIMEOUT):
                async with session.get(self._BITEFU_API, params={"d": d, "info": 1}) as resp:
                    resp.raise_for_status()
                    result = await resp.json(content_type=None)
        except Exception as e:
            _LOGGER.warning("_async_fetch_one_month %s: %s", d, e)
            return False

        if not isinstance(result, dict) or d not in result:
            _LOGGER.warning("_async_fetch_one_month: 响应中无 %s 数据", d)
            return False
        if not isinstance(result[d], dict) or not result[d]:
            _LOGGER.warning("_async_fetch_one_month: %s 数据格式错误或为空", d)
            return False

        self._replace_month(year_dict, month, result[d])
        return True

    def _replace_month(self, year_dict: dict, month: int, entries: dict) -> None:
        """Replace one month so corrected workdays do not keep an old holiday mark."""
        if not isinstance(entries, dict) or not entries:
            return
        prefix = f"{month:02d}"
        replacement = {}
        for key, info in entries.items():
            key = str(key)
            if not isinstance(info, dict) or not re.fullmatch(r"\d{4}", key):
                continue
            if not key.startswith(prefix) or "type" not in info:
                continue
            try:
                status = int(info["type"])
            except (TypeError, ValueError):
                continue
            if status in (0, 1, 2):
                replacement[key] = str(status)
        if not replacement:
            return
        # Keep the complete month: API type 0/1 must override fallback
        # holidays, and a corrected entry must erase the prior value.
        for key in list(year_dict):
            if isinstance(key, str) and len(key) == 4 and key.startswith(prefix):
                year_dict.pop(key, None)
        year_dict.update(replacement)

    # ------------------------------------------------------------------
    # 节假日状态查询
    # ------------------------------------------------------------------

    def is_holiday_status(self, date: datetime_class) -> int:
        """返回 0=工作日 1=休息日 2=法定节假日。
        
        此方法必须在 executor 线程中调用（包含阻塞 I/O）。
        """
        # 首次调用时从磁盘加载缓存，避免 __init__ 时做 I/O
        if not self._holiday_json:
            self.get_holidays_from_disk()
        y_str = str(date.year)
        h_dict = self._holiday_json.get(y_str, {})
        if not isinstance(h_dict, dict):
            h_dict = {}
        key = f"{date.month:02d}{date.day:02d}"
        # 法定节日的日期固定，但下一年度的调休安排可能尚未发布；
        # 远端接口此时可能把日期暂时标成普通工作日或休息日。
        holiday_date = date.date() if isinstance(date, datetime.datetime) else date
        known = self._known_holiday_dates({holiday_date.year})
        stored = None
        if key in h_dict:
            try:
                stored = int(h_dict[key])
            except (TypeError, ValueError):
                stored = None
        # Type 0 means an explicit workday. A routine rest day (type 1) does
        # not replace a statutory date; fixed-date fallback keeps it type 2.
        if stored == 0 and self._explicit_workday_overrides(holiday_date):
            return 0
        if stored == 2 or holiday_date in known:
            return 2
        if stored is not None:
            return stored
        # 没有特殊标注：周末=休息日，工作日=工作日
        return 1 if date.weekday() >= 5 else 0

    def is_holiday(self, date: datetime_class) -> str:
        return {0: "工作日", 1: "休息日", 2: "节假日"}[self.is_holiday_status(date)]

    def is_holiday_today(self) -> str:
        return self.is_holiday(self.today())

    def is_holiday_tomorrow(self) -> str:
        return self.is_holiday(self.tomorrow())

    # ------------------------------------------------------------------
    # 最近节日信息（放假安排详情）
    # ------------------------------------------------------------------

    def nearest_holiday_details(
        self, count: int = 2, min_days: int = 30, max_days: int | None = 45
    ) -> list[dict]:
        """返回最近若干次法定节假日的结构化放假安排。

        ``max_days`` 为 None 时不限制距离，只按日期由近到远取 ``count`` 个。
        下一年度安排尚未公布时，用固定法定日作为种子，避免假期块整段空白。
        """
        if not self._holiday_json:
            self.get_holidays_from_disk()
        today = self.today()
        details = []
        seen_periods: set[tuple[str, str]] = set()
        seeds: list[datetime_class] = []
        seen_days: set[datetime.date] = set()
        announced_days: set[datetime.date] = set()
        for y, dates in self._holiday_json.items():
            if y == "update_time" or not str(y).isdigit() or not isinstance(dates, dict):
                continue
            for m, t in dates.items():
                try:
                    if int(t) != 2:
                        continue
                    date = datetime_class.strptime(f"{y}-{m[0:2]}-{m[2:]}", "%Y-%m-%d")
                except (TypeError, ValueError):
                    continue
                seeds.append(date)
                seen_days.add(date.date())
                announced_days.add(date.date())

        years = {today.year, today.year + 1}
        years.update(day.year for day in seen_days)
        for known_date in self._known_holiday_dates(years):
            if known_date in seen_days:
                continue
            year_dict = self._holiday_json.get(str(known_date.year), {})
            mmdd = f"{known_date.month:02d}{known_date.day:02d}"
            if self._explicit_workday_overrides(known_date):
                continue
            seeds.append(datetime_class(known_date.year, known_date.month, known_date.day))
            seen_days.add(known_date)

        for date in sorted(seeds):
            y = str(date.year)
            m = date.strftime("%m%d")
            diff = (date - today).days
            if max_days is not None and diff > max_days:
                continue

            start = date
            end = date

            while self.is_holiday_status(start) != 0:
                start -= timedelta(days=1)
            while self.is_holiday_status(end) != 0:
                end += timedelta(days=1)

            start += timedelta(days=1)
            end -= timedelta(days=1)
            period_key = (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
            if period_key in seen_periods:
                continue
            seen_periods.add(period_key)

            if diff < min_days and not self._is_in_bridge_window(start, end, today):
                continue

            holiday_name = self._resolve_holiday_period_name(y, start, end, m)
            holiday_days = (end - start).days + 1
            schedule_published = any(
                start.date() <= announced <= end.date() for announced in announced_days
            )
            before_plan = self._build_bridge_plan(start, end, "before", today) if schedule_published else {}
            after_plan = self._build_bridge_plan(start, end, "after", today) if schedule_published else {}

            rows = []
            if before_plan:
                rows.append({
                    "label": "向前拼",
                    "range": before_plan["leave_range"],
                    "start": before_plan["leave_start"],
                    "end": before_plan["leave_end"],
                    "days": before_plan["leave_days"],
                    "total_days": before_plan["total_days"],
                    "calendar_days": before_plan["calendar_days"],
                })
            if after_plan:
                rows.append({
                    "label": "向后拼",
                    "range": after_plan["leave_range"],
                    "start": after_plan["leave_start"],
                    "end": after_plan["leave_end"],
                    "days": after_plan["leave_days"],
                    "total_days": after_plan["total_days"],
                    "calendar_days": after_plan["calendar_days"],
                })
            detail = {
                "name": holiday_name,
                "range": f"{start.month}/{start.day} - {end.month}/{end.day}",
                "start": start.strftime("%Y-%m-%d"),
                "end": end.strftime("%Y-%m-%d"),
                "days": holiday_days,
                "schedule_published": schedule_published,
                "title": f"{holiday_name}（{start.month}/{start.day} - {end.month}/{end.day}）",
                "rows": rows,
            }
            details.append(detail)
            if len(details) >= count:
                _LOGGER.debug("nearest_holiday_details: %s", details)
                return details
        _LOGGER.debug("nearest_holiday_details: %s", details)
        return details

    def nearest_holiday_detail(self, min_days: int = 30, max_days: int | None = 45) -> dict:
        """返回最近一次法定节假日的结构化放假安排。"""
        details = self.nearest_holiday_details(1, min_days, max_days)
        if details:
            return details[0]
        return {}

    def _is_in_bridge_window(
        self,
        holiday_start: datetime_class,
        holiday_end: datetime_class,
        today: datetime_class,
    ) -> bool:
        """判断今天是否已进入节前/节后拼假可展示窗口。"""
        before_start = self._bridge_edge_date(holiday_start, "before")
        after_end = self._bridge_edge_date(holiday_end, "after")
        if before_start and before_start <= today <= holiday_end:
            return True
        if after_end and holiday_start <= today <= after_end:
            return True
        return False

    def _bridge_edge_date(self, anchor: datetime_class, direction: str) -> datetime_class | None:
        """获取节前/节后拼假展示窗口的最外层边界日期。"""
        step = -1 if direction == "before" else 1
        cursor = anchor - timedelta(days=1) if direction == "before" else anchor + timedelta(days=1)

        saw_workday = False
        while self.is_holiday_status(cursor) == 0:
            saw_workday = True
            cursor += timedelta(days=step)

        if not saw_workday:
            return None

        edge = cursor - timedelta(days=step)
        while self.is_holiday_status(cursor) != 0:
            edge = cursor
            cursor += timedelta(days=step)

        return edge

    def _resolve_holiday_period_name(
        self,
        year: str,
        holiday_start: datetime_class,
        holiday_end: datetime_class,
        fallback_mmdd: str,
    ) -> str:
        """从整段假期中找最合适的节日名，避免返回“5月2日”这类泛名称。"""
        current = holiday_start
        while current <= holiday_end:
            mmdd = current.strftime("%m%d")
            name = self._resolve_holiday_name(year, mmdd)
            if not re.fullmatch(r"\d+月\d+日", name):
                return name
            current += timedelta(days=1)
        return self._resolve_holiday_name(year, fallback_mmdd)

    def _build_bridge_plan(
        self,
        holiday_start: datetime_class,
        holiday_end: datetime_class,
        direction: str,
        today: datetime_class,
    ) -> dict:
        """构建节前/节后拼假方案，补上紧邻的休息日和调休工作日。"""
        step = -1 if direction == "before" else 1
        cursor = holiday_start - timedelta(days=1) if direction == "before" else holiday_end + timedelta(days=1)

        leave_dates = []
        while self.is_holiday_status(cursor) == 0:
            leave_dates.append(cursor)
            cursor += timedelta(days=step)

        if not leave_dates:
            return {}

        extra_rest_dates = []
        while self.is_holiday_status(cursor) != 0:
            extra_rest_dates.append(cursor)
            cursor += timedelta(days=step)

        if direction == "before":
            leave_dates.reverse()
            extra_rest_dates.reverse()
            calendar_dates = extra_rest_dates + leave_dates
        else:
            calendar_dates = leave_dates + extra_rest_dates

        clip_anchor = today.replace(hour=0, minute=0, second=0, microsecond=0)
        if direction == "before" and calendar_dates and holiday_start <= clip_anchor <= holiday_end:
            calendar_dates = []
            leave_dates = []
            extra_rest_dates = []
        elif direction == "before" and calendar_dates and calendar_dates[0] <= clip_anchor <= holiday_end:
            calendar_dates = [d for d in calendar_dates if d >= clip_anchor]
            leave_dates = [d for d in leave_dates if d >= clip_anchor]
            extra_rest_dates = [d for d in extra_rest_dates if d >= clip_anchor]
        elif direction == "after" and calendar_dates and holiday_start <= clip_anchor <= calendar_dates[-1]:
            calendar_dates = [d for d in calendar_dates if d >= clip_anchor]
            leave_dates = [d for d in leave_dates if d >= clip_anchor]
            extra_rest_dates = [d for d in extra_rest_dates if d >= clip_anchor]

        if not calendar_dates:
            return {}

        calendar_days = []
        leave_set = {d.strftime("%Y-%m-%d") for d in leave_dates}
        rest_set = {d.strftime("%Y-%m-%d") for d in extra_rest_dates}
        for date in calendar_dates:
            key = date.strftime("%Y-%m-%d")
            if key in leave_set:
                tag = "请假"
                day_type = "leave"
            elif key in rest_set:
                tag = "休息"
                day_type = "rest"
            else:
                tag = "上班"
                day_type = "work"
            calendar_days.append({
                "key": key,
                "label": f"{date.month}/{date.day}",
                "tag": tag,
                "type": day_type,
            })

        leave_range = self._format_date_range(leave_dates)
        total_days = len(calendar_days) + (holiday_end - holiday_start).days + 1

        return {
            "leave_range": leave_range,
            "leave_start": leave_dates[0].strftime("%Y-%m-%d") if leave_dates else "",
            "leave_end": leave_dates[-1].strftime("%Y-%m-%d") if leave_dates else "",
            "leave_days": len(leave_dates),
            "total_days": total_days,
            "calendar_days": calendar_days,
        }

    def nearest_holiday_info(self, min_days: int = 30, max_days: int | None = 45) -> str:
        """返回最近一次法定节假日的放假安排说明。"""
        detail = self.nearest_holiday_detail(min_days, max_days)
        if not detail:
            return ""

        lines = [detail["title"]]
        for row in detail.get("rows", []):
            lines.append(
                f"{row['label']}：{row['range']}（{row['days']}天）👉 连休 {row['total_days']} 天"
            )
        info = "\n".join(lines)
        _LOGGER.debug("nearest_holiday_info: %s", info)
        return info

    def _format_range(self, days: list[dict]) -> str:
        """将日期列表格式化为 M/D - M/D。"""
        if not days:
            return ""
        start = days[0]["date"]
        end = days[-1]["date"]
        return f"{start.month}/{start.day} - {end.month}/{end.day}"

    def _format_date_range(self, dates: list[datetime_class]) -> str:
        """将 datetime 列表格式化为 M/D - M/D。"""
        if not dates:
            return ""
        start = dates[0]
        end = dates[-1]
        return f"{start.month}/{start.day} - {end.month}/{end.day}"

    # ------------------------------------------------------------------
    # getHoliday：兼容旧接口，返回 {date: 节日名} 字典
    # ------------------------------------------------------------------

    _KNOWN_SOLAR_HOLIDAYS = {
        "0101": "元旦",
        "0501": "劳动节",
        "1001": "国庆节",
        "1002": "国庆节",
        "1003": "国庆节",
    }
    _KNOWN_LUNAR_HOLIDAYS = {
        "0101": "春节",
        "0505": "端午节",
        "0815": "中秋节",
    }

    def _known_holiday_dates(self, years: set[int]) -> dict:
        """Return statutory holiday dates when next year's official notice is absent.

        The public API only marks announced days as ``type=2``. Around the end
        of a year it can therefore leave January 1 out of the cache until the
        State Council publishes the next year's arrangement. Explicit cache
        values still win: type 0 stays a workday, and type 2 keeps its record.
        """
        cache = self.__dict__.setdefault("_known_holiday_cache", {})
        missing = [year for year in years if year not in cache]
        if missing:
            computed = self._compute_known_holiday_dates(set(missing))
            for year in missing:
                cache[year] = {}
            for date, name in computed.items():
                cache.setdefault(date.year, {})[date] = name
        result: dict = {}
        for year in years:
            result.update(cache.get(year, {}))
        return result

    def _compute_known_holiday_dates(self, years: set[int]) -> dict:
        results: dict = {}
        for year in years:
            for mmdd, name in self._KNOWN_SOLAR_HOLIDAYS.items():
                try:
                    date = datetime_class.strptime(f"{year}{mmdd}", "%Y%m%d").date()
                    results[date] = name
                except ValueError:
                    continue

            # 春节法定假期从除夕到正月初三；以正月初一换算，再
            # 向前一天、向后两天生成基础法定日期，不推测额外调休。
            try:
                lunar_new_year = lunar.CalendarToday.lunar_to_solar(year, 1, 1)
                for offset in range(-1, 3):
                    results[lunar_new_year + timedelta(days=offset)] = "春节"
            except (TypeError, ValueError):
                _LOGGER.debug("无法换算 %s 年春节", year)

            for mmdd, name in self._KNOWN_LUNAR_HOLIDAYS.items():
                if mmdd == "0101":
                    continue
                try:
                    month, day = int(mmdd[:2]), int(mmdd[2:])
                    date = lunar.CalendarToday.lunar_to_solar(year, month, day)
                    results[date] = name
                except (TypeError, ValueError):
                    _LOGGER.debug("无法换算农历节日 %s-%s", year, mmdd)

            # 清明是节气，不是固定公历日；使用现有节气计算器。
            try:
                for item in lunar.jieqi().creat_year_jieqi(year):
                    if item.get("name") != "清明":
                        continue
                    date = datetime_class.strptime(item["time"], "%Y-%m-%d").date()
                    results[date] = "清明节"
                    break
            except (TypeError, ValueError, IndexError):
                _LOGGER.debug("无法计算 %s 年清明节", year)
        return results

    def getHoliday(self, days: int = 1) -> dict:
        """返回 {datetime.date: 节日名称} 字典（用于寻找最近节日）。"""
        if not self._holiday_json:
            self.get_holidays_from_disk()

        results = {}
        years: set[int] = set()
        today = self.today()
        years.update({today.year, today.year + 1})
        for y, dates in self._holiday_json.items():
            if y == "update_time" or not str(y).isdigit():
                continue
            try:
                years.add(int(y))
            except (TypeError, ValueError):
                continue
            if not isinstance(dates, dict):
                continue
            for m, t in dates.items():
                try:
                    if int(t) != 2:
                        continue
                    d_str = "{}-{}-{}".format(y, m[0:2], m[2:])
                except (TypeError, ValueError, IndexError):
                    continue
                try:
                    d = datetime_class.strptime(d_str, "%Y-%m-%d").date()
                    results[d] = self._resolve_holiday_name(y, m)
                except (TypeError, ValueError):
                    continue

        # Fill only missing dates. A cached workday (type 0) stays authoritative.
        for date, name in self._known_holiday_dates(years).items():
            year_dict = self._holiday_json.get(str(date.year), {})
            mmdd = f"{date.month:02d}{date.day:02d}"
            if self._explicit_workday_overrides(date):
                continue
            results.setdefault(date, name)
        return self._apply_period_names(results)

    def _explicit_workday_overrides(self, date: datetime.date) -> bool:
        """Return whether cached type 0 should replace one statutory date.

        Before the official notice, the API marks an ordinary weekday such as
        January 1 as type 0. That is not a makeup workday. Once a statutory
        day is announced within ten days, an explicit type 0 belongs to the
        published arrangement and stays a workday.
        """
        year_dict = self._holiday_json.get(str(date.year), {})
        if not isinstance(year_dict, dict):
            return False
        mmdd = f"{date.month:02d}{date.day:02d}"
        if str(year_dict.get(mmdd)) != "0":
            return False
        for key, value in year_dict.items():
            text = str(key)
            try:
                if int(value) != 2 or len(text) != 4 or not text.isdigit():
                    continue
                announced = datetime.date(date.year, int(text[:2]), int(text[2:]))
            except (TypeError, ValueError):
                continue
            if abs((announced - date).days) <= 10:
                return True
        return False

    def _apply_period_names(self, results: dict) -> dict:
        """Name unnamed days inside a continuous holiday after the named day."""
        if not results:
            return results
        ordered = sorted(results)
        groups: list[list] = [[ordered[0]]]
        for date in ordered[1:]:
            if (date - groups[-1][-1]).days == 1:
                groups[-1].append(date)
            else:
                groups.append([date])
        preferred_names = ("春节", "国庆节", "劳动节", "清明节", "端午节", "中秋节", "元旦")
        for group in groups:
            names = [
                results[date]
                for date in group
                if not re.fullmatch(r"\d+月\d+日", str(results[date]))
            ]
            if not names:
                continue
            chosen = next((name for name in names if name in preferred_names), None)
            if chosen is None:
                chosen = next((name for name in names if "节" in name), names[0])
            for date in group:
                if re.fullmatch(r"\d+月\d+日", str(results[date])):
                    results[date] = chosen
        return results

    def _resolve_holiday_name(self, year: str, mmdd: str) -> str:
        """根据年份和 MMDD 推断节日名称（使用内置节日映射）。"""
        _HOLIDAY_NAMES = {
            "0101": "元旦",
            "0214": "情人节",
            "0308": "妇女节",
            "0401": "愚人节",
            "0501": "劳动节",
            "0601": "儿童节",
            "0701": "建党节",
            "0801": "建军节",
            "0910": "教师节",
            "1001": "国庆节",
            "1002": "国庆节",
            "1003": "国庆节",
            "1004": "国庆节",
            "1005": "国庆节",
            "1006": "国庆节",
            "1007": "国庆节",
            "1231": "元旦前夕",
        }
        if mmdd in _HOLIDAY_NAMES:
            return _HOLIDAY_NAMES[mmdd]

        month = int(mmdd[:2])
        day = int(mmdd[2:])
        year_int = int(year)

        # 兼容旧逻辑：未命中固定映射时，再按公历节日/农历节日/节气动态解析。
        names = []

        solar_name = lunar.Festival.solar_Fstv_for_year(month, day, year_int)
        if solar_name:
            names.extend([name.strip() for name in solar_name.split(",") if name.strip()])

        try:
            lunar_date = lunar.LunarDate.fromSolarDate(year_int, month, day)
            lunar_name = lunar.Festival.lunar_Fstv(lunar_date.month, lunar_date.day)
            if lunar_name:
                names.extend([name.strip() for name in lunar_name.split(",") if name.strip()])
        except Exception as err:
            _LOGGER.debug("resolve lunar holiday name failed for %s-%s: %s", year, mmdd, err)

        try:
            for item in lunar.jieqi().creat_year_jieqi(year_int):
                comps = item["time"].split("-")
                if item["name"] == "清明" and int(comps[1]) == month and int(comps[2]) == day:
                    names.append("清明节")
                    break
        except Exception as err:
            _LOGGER.debug("resolve term holiday name failed for %s-%s: %s", year, mmdd, err)

        seen = set()
        ordered = []
        for name in names:
            if name not in seen:
                seen.add(name)
                ordered.append(name)

        if ordered:
            preferred = next((name for name in ordered if "节" in name), ordered[0])
            if preferred == "元旦节":
                return "元旦"
            if preferred == "国际儿童节":
                return "儿童节"
            return preferred

        return f"{month}月{day}日"


def main():
    pass


if __name__ == "__main__":
    main()
