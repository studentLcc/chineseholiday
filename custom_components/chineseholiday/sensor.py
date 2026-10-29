# coding=utf-8
"""中国节假日传感器 - 版本 0.3.2-pre"""
from __future__ import annotations

import datetime
from datetime import timedelta
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.sensor import PLATFORM_SCHEMA, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import event as evt
from homeassistant.helpers.entity import generate_entity_id
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DEFAULT_NAME, DOMAIN
from .coordinator import ChineseHolidayCoordinator
from . import holiday
from . import lunar

_LOGGER = logging.getLogger(__name__)

CONF_UPDATE_INTERVAL = "update_interval"
CONF_SOLAR_ANNIVERSARY = "solar_anniversary"
CONF_LUNAR_ANNIVERSARY = "lunar_anniversary"
CONF_CALCULATE_AGE = "calculate_age"
CONF_CALCULATE_AGE_DATE = "date"
CONF_CALCULATE_AGE_NAME = "name"
CONF_NOTIFY_SCRIPT_NAME = "notify_script_name"
CONF_NOTIFY_TIMES = "notify_times"
CONF_NOTIFY_PRINCIPLES = "notify_principles"
CONF_NOTIFY_PRINCIPLES_DATE = "date"
CONF_NOTIFY_PRINCIPLES_NAME = "name"
CONF_NOTIFY_PRINCIPLES_SOLAR = "solar"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_NOTIFY_TIMES, default=["09:00:00"]): [cv.time],
        vol.Optional(CONF_NOTIFY_SCRIPT_NAME, default=""): cv.string,
        vol.Optional("show_detail", default=True): cv.boolean,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_SOLAR_ANNIVERSARY, default={}): {str: [str]},
        vol.Optional(CONF_LUNAR_ANNIVERSARY, default={}): {str: [str]},
        vol.Optional(CONF_CALCULATE_AGE, default=[]): [
            {
                vol.Optional(CONF_CALCULATE_AGE_DATE): cv.string,
                vol.Optional(CONF_CALCULATE_AGE_NAME): cv.string,
            }
        ],
        vol.Optional(CONF_NOTIFY_PRINCIPLES, default={}): {
            str: [
                {
                    vol.Optional(CONF_NOTIFY_PRINCIPLES_NAME, default=""): cv.string,
                    vol.Optional(CONF_NOTIFY_PRINCIPLES_DATE, default=""): cv.string,
                    vol.Optional(CONF_NOTIFY_PRINCIPLES_SOLAR, default=True): cv.boolean,
                }
            ]
        },
        vol.Optional(
            CONF_UPDATE_INTERVAL, default=timedelta(hours=8)
        ): (vol.All(cv.time_period, cv.positive_timedelta)),
    }
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: dict,
    async_add_entities: AddEntitiesCallback,
    discovery_info=None,
) -> None:
    """YAML 方式配置（向后兼容）。"""
    coordinator = ChineseHolidayCoordinator(
        hass,
        {
            "name": config[CONF_NAME],
            "notify_times": config.get(CONF_NOTIFY_TIMES, [datetime.time(9, 0, 0)]),
            "notify_script_name": config.get(CONF_NOTIFY_SCRIPT_NAME, ""),
            "show_detail": config.get("show_detail", True),
            "solar_anniversary": config.get(CONF_SOLAR_ANNIVERSARY, {}),
            "lunar_anniversary": config.get(CONF_LUNAR_ANNIVERSARY, {}),
            "calculate_age": config.get(CONF_CALCULATE_AGE, []),
            "notify_principles": config.get(CONF_NOTIFY_PRINCIPLES, {}),
            "cache_path": hass.config.path("chineseholiday_holidays.json"),
            "update_interval": config.get(CONF_UPDATE_INTERVAL, timedelta(hours=8)),
        },
    )
    await coordinator.async_config_entry_first_refresh()
    async_add_entities([ChineseHolidaySensor(coordinator)], True)


def _parse_notify_times(times_str: str) -> list[datetime.time]:
    """将逗号分隔的时间字符串解析为 datetime.time 列表。"""
    result = []
    for t in times_str.split(","):
        t = t.strip()
        if not t:
            continue
        try:
            result.append(datetime.time.fromisoformat(t))
        except ValueError:
            _LOGGER.warning("notify_times 格式错误，跳过：%s", t)
    return result or [datetime.time(9, 0, 0)]


def _entry_get(entry: ConfigEntry, key: str, default):
    """从 options 优先，再从 data 取值。"""
    return entry.options.get(key, entry.data.get(key, default))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Config entry 方式配置（UI 添加）。"""
    async_add_entities([ChineseHolidaySensor(entry.runtime_data)], True)


class ChineseHolidaySensor(CoordinatorEntity, SensorEntity):
    """中国节假日传感器实体。

    state 输出：工作日 / 休息日 / 节假日
    通过 native_value 提供状态（HA 2024+ SensorEntity 要求）
    """

    _attr_icon = "mdi:calendar-today"
    _attr_should_poll = False
    # 非数值型 sensor 不设 device_class / unit_of_measurement

    def __init__(
        self,
        coordinator: ChineseHolidayCoordinator,
    ) -> None:
        super().__init__(coordinator)
        config = coordinator.config
        notify_times = config.get("notify_times", [datetime.time(9, 0)])
        if isinstance(notify_times, str):
            notify_times = _parse_notify_times(notify_times)
        self.client_name = config.get("name", DEFAULT_NAME)
        self._attr_name = self.client_name
        self._attr_unique_id = config.get("unique_id")
        self._show_detail = config.get("show_detail", True)
        self._hass = coordinator.hass
        self._script_name = config.get("notify_script_name", "")
        self._notify_times = notify_times
        # 不在 __init__（事件循环）里做任何 I/O
        # Holiday() 和 CalendarToday() 在 _update()（executor 线程）里懒加载
        self._holiday: holiday.Holiday | None = coordinator.holiday
        self._lunar: lunar.CalendarToday | None = None
        self.attributes: dict[str, Any] = {}
        self.localizedAttributes: dict[str, Any] = {}
        self._solar_anniversary = config.get("solar_anniversary", {})
        self._lunar_anniversary = config.get("lunar_anniversary", {})
        self._calculate_age = config.get("calculate_age", [])
        self._notify_principles = config.get("notify_principles", {})
        self._cache_path = config.get("cache_path")
        self._remove_update_listener = None
        self._remove_notify_listener = None
        self._now = dt_util.now()
        self._last_notification_signature: tuple[str, str] | None = None
        self.entity_id = config.get("entity_id") or generate_entity_id(
            "sensor.{}", self.client_name, hass=self._hass
        )
        self._apply_coordinator_data()
        # Timers are registered in async_added_to_hass and removed on unload.

    def _apply_coordinator_data(self) -> None:
        data = self.coordinator.data or {}
        if "state" in data:
            self._attr_native_value = data.get("state")
            self.localizedAttributes = data.get("attributes", {})

    @callback
    def _handle_coordinator_update(self) -> None:
        """Apply coordinator payload using the current entity update contract."""
        self._apply_coordinator_data()
        super()._handle_coordinator_update()

    @property
    def name(self) -> str:
        return self._attr_name

    @property
    def native_value(self) -> str | None:
        """HA 2024+ SensorEntity 通过 native_value 暴露状态。

        SensorEntity.state 是 @final，子类不能覆盖，必须用 native_value。
        """
        return self._attr_native_value  # type: ignore[return-value]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.localizedAttributes

    # ------------------------------------------------------------------
    # 定时器：每天凌晨 00:00:15 强制更新
    # ------------------------------------------------------------------

    def _setup_update_listener(self) -> None:
        @callback
        def _listener(_):
            self._setup_update_listener()
            self._hass.async_create_task(self.coordinator.async_request_refresh())

        now = dt_util.now()
        next_midnight = (
            now.replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=1, seconds=15)
        )
        if self._remove_update_listener:
            self._remove_update_listener()
        self._remove_update_listener = evt.async_track_point_in_time(
            self._hass, _listener, next_midnight
        )

    # ------------------------------------------------------------------
    # 定时器：按配置时间触发通知脚本
    # ------------------------------------------------------------------

    def _setup_notify_listener(self) -> None:
        @callback
        def _listener(_):
            self._setup_notify_listener()
            self._hass.async_create_task(self.async_notify())

        now = dt_util.now()
        notify_dates = []
        for t in sorted(self._notify_times):
            notify_dates.append(now.replace(
                hour=t.hour, minute=t.minute, second=t.second, microsecond=0
            ))

        if not notify_dates:
            return
        notify_d = next((d for d in notify_dates if d > now), None)
        if not notify_d:
            notify_d = notify_dates[0] + timedelta(days=1)

        if self._remove_notify_listener:
            self._remove_notify_listener()
        self._remove_notify_listener = evt.async_track_point_in_time(
            self._hass, _listener, notify_d
        )

    async def async_added_to_hass(self) -> None:
        """Register timers only after HA owns the entity."""
        await super().async_added_to_hass()
        self._setup_update_listener()
        self._setup_notify_listener()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel timers when the entity is unloaded or reloaded."""
        if self._remove_update_listener:
            self._remove_update_listener()
            self._remove_update_listener = None
        if self._remove_notify_listener:
            self._remove_notify_listener()
            self._remove_notify_listener = None
        await super().async_will_remove_from_hass()

    async def async_request_update(self) -> None:
        """Ask the shared coordinator to refresh."""
        await self.coordinator.async_request_refresh()

    async def async_update(self) -> None:
        """Refresh the entity using HA's async entity contract."""
        await self.coordinator.async_request_refresh()

    async def async_get_holidays(self, force: bool = True) -> dict[str, Any]:
        """Fetch holiday data for this sensor's coordinator."""
        return await self.coordinator.async_get_holidays(force)

    # ------------------------------------------------------------------
    # 通知脚本
    # ------------------------------------------------------------------

    async def async_notify(self) -> None:
        """调用 HA 脚本发送节日提醒。"""
        self._now = dt_util.now()
        if not self._script_name or not self._notify_principles:
            return
        if self._lunar is None:
            _LOGGER.warning("notify: _lunar 尚未初始化，跳过本次通知")
            return

        dates = await self._hass.async_add_executor_job(self._dates_need_to_notify)
        messages = []
        for item in dates:
            days = item["day"]
            fes_list = item["list"]
            if days == 0:
                messages.append("今天是 " + ",".join(fes_list))
            else:
                messages.append(
                    "距离 " + ",".join(fes_list) + "还有" + str(days) + "天"
                )
        if messages:
            message = ",".join(messages)
            signature = (self._now.date().isoformat(), message)
            if signature == self._last_notification_signature:
                _LOGGER.debug("跳过重复节日通知：%s", message)
                return
            try:
                await self._hass.services.async_call(
                    "script", self._script_name, {"message": message}
                )
                self._last_notification_signature = signature
            except Exception as e:
                _LOGGER.error("notify: 调用脚本失败: %s", e)

    def notify(self) -> None:
        """Compatibility wrapper for callers of the old synchronous method."""
        self._hass.async_create_task(self.async_notify())

    def _dates_need_to_notify(self) -> list[dict]:
        dates = []
        current_year = self._lunar.solar()[0]
        for key, value in self._notify_principles.items():
            days_keys = key.split("|")
            for item in value:
                date = item.get("date", "")
                solar = item.get("solar", True)
                name = item.get("name", "")
                fes_date = None
                fes_list: list[str] = []

                if name:
                    try:
                        fes_list = [name]
                        date_str = (
                            str(current_year)
                            + lunar.Festival._weekday_festival_reserse[name]
                        )
                        fes_date = datetime.datetime.strptime(
                            date_str, "%Y%m%d"
                        ).date()
                    except Exception:
                        pass
                elif date:
                    try:
                        date_key = str(date)
                        lookup_key = date_key[-4:]
                        if solar:
                            year = int(date_key[:4]) if len(date_key) == 8 else current_year
                            date_str = str(year) + lookup_key
                            fes_date = datetime.datetime.strptime(date_str, "%Y%m%d").date()
                            anniversary_names = self._solar_anniversary.get(date_key, [])
                            if len(date_key) == 8 and not anniversary_names:
                                anniversary_names = self._solar_anniversary.get(lookup_key, [])
                            fes_list = list(lunar.Festival._solar_festival.get(lookup_key, []))
                            fes_list += anniversary_names
                        else:
                            year = int(date_key[:4]) if len(date_key) == 8 else current_year
                            month, day = int(lookup_key[:2]), int(lookup_key[2:])
                            fes_date = lunar.CalendarToday.lunar_to_solar(year, month, day)
                            fes_list = list(lunar.Festival._lunar_festival.get(lookup_key, []))
                            anniversary_names = self._lunar_anniversary.get(date_key, [])
                            if len(date_key) == 8 and not anniversary_names:
                                anniversary_names = self._lunar_anniversary.get(lookup_key, [])
                            fes_list += anniversary_names
                    except (TypeError, ValueError):
                        _LOGGER.warning("忽略无效通知日期：%s", date)
                        continue

                if fes_date and fes_list:
                    today = self._now.date()
                    diff = (fes_date - today).days
                    if str(diff) in days_keys:
                        dates.append({**item, "day": diff, "list": fes_list})
        return dates

    # ------------------------------------------------------------------
    # 纪念日计算
    # ------------------------------------------------------------------

    def _anniversary_label(self, names: list[str], age: int) -> str:
        if age == -1:
            return ",".join(names)
        return ",".join(
            f"{n}({age}岁)" if "生日" in n else f"{n}({age}周年)" for n in names
        )

    def calculate_anniversary(self, count: int = 1) -> list[tuple]:
        anniversaries: dict[str, list] = {}
        current_solar_year = self._lunar.solar()[0]
        current_lunar_year, current_lunar_month, current_lunar_day = self._lunar.lunar()

        for key, value in self._lunar_anniversary.items():
            if len(key) == 8:
                year, month, day = int(key[:4]), int(key[4:6]), int(key[6:])
                age = lunar.CalendarToday.get_age_by_birth_lunar_to_solar(
                    year, month, day, self._now.date()
                ) + 1
            else:
                month, day = int(key[:2]), int(key[2:])
                age = -1
            y = current_lunar_year
            if month < current_lunar_month:
                y += 1
            elif month == current_lunar_month and day < current_lunar_day:
                y += 1
            solar_date = lunar.CalendarToday.lunar_to_solar(y, month, day)
            date_str = solar_date.strftime("%Y%m%d")
            anniversaries.setdefault(date_str, []).append(
                {"anniversary": self._anniversary_label(value, age), "solar": False}
            )

        for key, value in self._solar_anniversary.items():
            if len(key) == 8:
                year, month, day = int(key[:4]), int(key[4:6]), int(key[6:])
                key = key[4:]
                age = lunar.CalendarToday.get_age_by_birth_solar(
                    year, month, day, self._now.date()
                ) + 1
            else:
                age = -1
            date_str = str(current_solar_year) + key
            anniversaries.setdefault(date_str, []).append(
                {"anniversary": self._anniversary_label(value, age), "solar": True}
            )

        today = self._now.date()
        results = []
        for key, annis in sorted(anniversaries.items()):
            last_update = datetime.datetime.strptime(key, "%Y%m%d").date()
            diff = (last_update - today).days
            if diff > 0 and len(results) < count:
                results.append((key, diff, annis))
        return results

    def custom_anniversary(self) -> str:
        l_month = self._lunar.lunar()[1]
        l_day = self._lunar.lunar()[2]
        s_month = self._lunar.solar()[1]
        s_day = self._lunar.solar()[2]
        l = lunar.festival_handle(self._lunar_anniversary, l_month, l_day) or ""
        s = lunar.festival_handle(self._solar_anniversary, s_month, s_day) or ""
        return l + s

    def calculate_age(self) -> None:
        if not self._calculate_age:
            return
        now_day = self._now.replace(tzinfo=None)
        past_dates = []
        future_dates = []
        past_count = 0
        future_count = 0

        for item in self._calculate_age:
            date = item[CONF_CALCULATE_AGE_DATE]
            name = item[CONF_CALCULATE_AGE_NAME]
            key = datetime.datetime.strptime(date, "%Y-%m-%d %H:%M:%S")

            if (now_day - key).total_seconds() > 0:
                ts = int((now_day - key).total_seconds())
                y, r = divmod(ts, 86400 * 365)
                d, r = divmod(r, 86400)
                h, r = divmod(r, 3600)
                m, s = divmod(r, 60)
                desc = f"{y}年{d}天{h}小时{m}分钟{s}秒"
                self.localizedAttributes[f"{past_count+1}.过去纪念日"] = name
                self.localizedAttributes[f"{past_count+1}.过去纪念日日期"] = date
                self.localizedAttributes[f"{past_count+1}.已经过去"] = desc
                past_dates.append({"name": name, "date": date, "interval": ts, "description": desc})
                past_count += 1

                counter = 0
                while (now_day - key).total_seconds() > 0:
                    date = str(key.year + 1) + date[4:]
                    key = datetime.datetime.strptime(date, "%Y-%m-%d %H:%M:%S")
                    counter += 1
                name = name + " " + str(counter) + ("周岁" if "生日" in name else "周年")

            if (now_day - key).total_seconds() < 0:
                ts = int((key - now_day).total_seconds())
                y, r = divmod(ts, 86400 * 365)
                d, r = divmod(r, 86400)
                h, r = divmod(r, 3600)
                m, s = divmod(r, 60)
                desc = f"{y}年{d}天{h}小时{m}分钟{s}秒"
                self.localizedAttributes[f"{future_count+1}.未来纪念日"] = name
                self.localizedAttributes[f"{future_count+1}.未来纪念日日期"] = date
                self.localizedAttributes[f"{future_count+1}.还有"] = desc
                future_dates.append({"name": name, "date": date, "interval": ts, "description": desc})
                future_count += 1

        self.attributes["past_dates"] = past_dates
        self.attributes["future_dates"] = future_dates

    def nearest_holiday(self) -> dict:
        now_day = self._now.date()
        results = self._holiday.getHoliday()
        count_dict = {k: (k - now_day).days for k in results if (k - now_day).days > 0}
        if not count_dict:
            return {}
        nearest = min(count_dict)
        return {
            "name": results[nearest],
            "date": nearest.isoformat(),
            "day": str((nearest - now_day).days),
        }

    # ------------------------------------------------------------------
    # 主更新方法（在 executor 线程中运行）
    # ------------------------------------------------------------------

    def _update(self) -> None:
        """在 executor 线程中运行，允许阻塞 I/O。"""
        _LOGGER.info("chineseholiday: updating...")
        self._now = dt_util.now()
        self.attributes = {}
        self.localizedAttributes = {}
        # 懒加载：第一次 update 时初始化（此时已在 executor 线程，可以做阻塞 I/O）
        if self._holiday is None:
            self._holiday = holiday.Holiday(self._cache_path)
        if self._lunar is None:
            self._lunar = lunar.CalendarToday(self._now.replace(tzinfo=None))
        else:
            self._lunar = lunar.CalendarToday(self._now.replace(tzinfo=None))

        # HA 2024+ SensorEntity：必须写 _attr_native_value，不能覆盖 state 属性
        self._attr_native_value = self._holiday.is_holiday_today()

        self.attributes["tomorrow_state"] = self._holiday.is_holiday_tomorrow()
        self.localizedAttributes["明天"] = self._holiday.is_holiday_tomorrow()

        self.attributes["solar"] = self._lunar.solar_date_description()
        self.localizedAttributes["今天"] = self._lunar.solar_date_description()
        self.attributes["week"] = self._lunar.week_description()
        self.localizedAttributes["星期"] = self._lunar.week_description()
        self.attributes["lunar"] = self._lunar.lunar_date_description()
        self.localizedAttributes["农历"] = self._lunar.lunar_date_description()
        self.attributes["week_number"] = self._lunar.solar_week_number()
        self.localizedAttributes["周数"] = self._lunar.solar_week_number_description()

        term = self._lunar.solar_Term()
        if term:
            self.attributes["term"] = term
            self.localizedAttributes["节气"] = term

        festival = self._lunar.festival_description()
        if festival:
            self.attributes["festival"] = festival
            self.localizedAttributes["节日"] = festival

        custom = self.custom_anniversary()
        if custom:
            self.attributes["anniversary"] = custom
            self.localizedAttributes["纪念日"] = custom

        results = self.calculate_anniversary(5)
        self.attributes["next_anniversaries"] = []
        self.localizedAttributes["接下来的纪念日"] = []

        for i, (key, days, annis) in enumerate(results):
            s = "".join(a["anniversary"] for a in annis)
            if i == 0:
                self.attributes["nearest_anniversary"] = s
                self.localizedAttributes["最近的纪念日"] = s
                self.attributes["nearest_anniversary_date"] = key
                self.localizedAttributes["最近的纪念日日期"] = key
                self.attributes["nearest_anniversary_days"] = days
                self.localizedAttributes["最近的纪念日还有"] = f"{days}天"
            else:
                self.attributes["next_anniversaries"].append(
                    {"date": key, "name": s, "days": days}
                )
                self.localizedAttributes["接下来的纪念日"].append(
                    f"距离纪念日 {s}-{key} 还有 {days} 天 "
                )

        nearest = self.nearest_holiday()
        if nearest:
            self.attributes["nearest_holiday"] = nearest["name"]
            self.localizedAttributes["最近的节日"] = nearest["name"]
            self.attributes["nearest_holiday_date"] = nearest["date"]
            self.localizedAttributes["最近的节日日期"] = nearest["date"]
            self.attributes["nearest_holiday_days"] = int(nearest["day"])
            self.localizedAttributes["最近的节日还有"] = f"{nearest['day']}天"

        self.calculate_age()

        details = self._holiday.nearest_holiday_details(2, 0, 45)
        if details:
            self.attributes["holiday_info_details"] = details
            self.localizedAttributes["节假日放假详情结构化列表"] = details
            self.attributes["holiday_info_detail"] = details[0]
            self.localizedAttributes["节假日放假详情结构化"] = details[0]

        info = self._holiday.nearest_holiday_info(0, 45)
        if info:
            self.attributes["holiday_info"] = info
            self.localizedAttributes["节假日放假详情"] = info

        if self._show_detail:
            self.localizedAttributes["data"] = self.attributes
