import hashlib
import json
import logging
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .external_sources import (
    FetchError,
    fetch_bytes,
    fetch_json,
    parse_feed,
    parse_gdelt,
    parse_geojson,
    parse_html_list,
    parse_json_api,
    validate_public_url,
)
from .text_cleaning import plain_text

_logger = logging.getLogger(__name__)


REGIONS = {"heyuan", "guangdong", "national", "global"}
SOURCE_CATEGORIES = {
    "gov", "water", "housing", "procurement", "industry", "news",
    "finance", "general", "legacy",
}
SOURCE_KINDS = {"rss", "gdelt", "html_list", "json_api", "geojson"}
# P4: 信源分级——T1官方源 / T2权威媒体 / T3聚合或一般 / T4未验证
SOURCE_TIERS = {"T1", "T2", "T3", "T4"}
TIER_RELIABILITY = {"T1": 0.9, "T2": 0.7, "T3": 0.5, "T4": 0.3}

# v8：全球态势图层的图层白名单与中文名。图层是**封闭集合** —— 接口把非法 layer
# 当作参数错误（400）而不是"查不到就返回空"，这样拼错图层时能立刻看见，
# 而不是以为"这个世界很太平"。`other` 兜底：解析器认不出类别时归到它。
SITUATION_LAYER_NAMES = {
    "quake": "地震",
    "disaster": "灾害",
    "wildfire": "野火",
    "storm": "风暴",
    "flood": "洪水",
    "volcano": "火山",
    "drought": "干旱",
    "other": "其他",
}
#: 态势层专用来源的刷新间隔区间（分钟）。团队约定 20~30，避免一天几百条被高频重复抓。
SITUATION_MIN_REFRESH_MINUTES = 20
SITUATION_MAX_REFRESH_MINUTES = 30
SITUATION_SOURCE_KIND = "geojson"

# 抓取失败退避：delay = base × 2^(failures-1)，封顶 max。成功抓取即把
# `consecutive_failures` 归零，退避自然重置（既有行为，不改）。
#
# 2026-09-20 拍板：原封顶 60 分钟太短。对端持续不可达时，60 分钟封顶意味着每
# 小时都会再去撞一次；GDELT 实测 575 次抓取里 448 次是"不可达"（TLS 握手超时），
# 其中绝大多数都是明知不可达后的无谓重试。封顶拉到 360 分钟，把重试成本按
# 2^(failures-1) 指数拉开。**不自动停用**：源仍保持 enabled，只是抓得越来越稀。
FAILURE_BACKOFF_BASE_MINUTES = 15
FAILURE_BACKOFF_MAX_MINUTES = 360

#: 单次 `refresh_due_sources` 的**时间预算**（秒）—— 到点即停，剩余到期源留到下一轮。
#:
#: **为什么需要**：调度器是单线程串行（`radar_scheduler._run` 里 situation →
#: external → cognition → … 一个接一个）。改造前 `refresh_due_sources` 会在
#: **一次调用里**把到期源全部抓完，而冷启动时几乎所有常规源都已到期。那个窗口里
#: 循环轮不到认知/备份，首页的只读接口也拿不到响应 —— 用户可感的说法就是
#: **"刚开机那段时间首页不响应"**。
#:
#: **取值依据**（实测见 `build-artifacts/v152/c-baseline-real.txt`，真库只读）：
#:
#:   冷启动到期源数        35 个（隔夜 39 个 = 启用总数）
#:   单源耗时（近 7 天）    ok  n=2701 median 2.4s / p90 5.1s / max 68.9s
#:                          error n=255  median 4.1s / p90 14.0s / max 60.6s
#:   ⇒ 常态一次 pass       ≈ 35 × 2.5s  ≈ **88 s**
#:   ⇒ 病态一次 pass       ≈ 35 × 60s+  ≈ **35 min**（对端大面积 TLS/连接超时）
#:
#: 取 **120 s**：① 常态（88 s）仍然**一次抓完**，不为健康网络增加任何额外延迟；
#: ② 病态最坏情况从 ~35 min 压到 ~2 min（**17 倍**），任何一次迭代都不再独占循环；
#: ③ 与调度节奏同量级（external 轮询 30 s、cognition 300 s），不会因为预算过小
#: 把常态也切碎成好几轮（那只会让"每个源最终都会被抓到"变慢）。
COLLECT_PASS_BUDGET_SECONDS = 120.0


def _failure_backoff_minutes(failures: int) -> int:
    """连续失败 `failures` 次后的重试间隔（分钟）。

    单独抽成函数，是为了让"退避序列"只有一处定义 —— 采集条目路径
    (`refresh_source`) 与态势图层路径 (`refresh_situation_source`) 共用它，
    不会各自漂移。
    """
    failures = max(1, int(failures))
    return min(
        FAILURE_BACKOFF_MAX_MINUTES,
        FAILURE_BACKOFF_BASE_MINUTES * (2 ** (failures - 1)),
    )


def utc_now():
    return datetime.now(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def canonicalize_url(url):
    parts = urlsplit(validate_public_url(url))
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"spm", "from", "source"}
    ]
    host = (parts.hostname or "").lower()
    port = parts.port
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", urlencode(query), ""))


def fetch_source(source):
    kind = source["kind"]
    if kind == "json_api":
        config = json.loads(source.get("config_json") or "{}")
        body = fetch_json(source["endpoint"], config.get("request_payload", {}))
        return parse_json_api(body, source["source_id"], source["name"], config)
    if kind == SITUATION_SOURCE_KIND:
        # 态势层：走同一套 fetch_bytes 安全网（公网/DNS 校验、10s 超时、5MB 上限），
        # 但产出的是 SituationPoint —— 与 external_items 的写入链在类型上分离。
        config = json.loads(source.get("config_json") or "{}")
        body = fetch_bytes(source["endpoint"])
        return parse_geojson(body, source["source_id"], source["name"], config)
    body = fetch_bytes(source["endpoint"])
    if kind == "rss":
        return parse_feed(body, source["source_id"], source["name"], source["endpoint"])
    if kind == "gdelt":
        return parse_gdelt(body, source["source_id"], source["name"])
    if kind == "html_list":
        return parse_html_list(body, source["source_id"], source["name"], source["endpoint"])
    raise FetchError("unsupported", f"不支持的数据源类型：{kind}")


class ExternalRadarService:
    def __init__(
        self,
        database,
        fetcher=fetch_source,
        now=utc_now,
        on_item_stored=None,
        pass_budget_seconds=None,
    ):
        self.database = database
        self.fetcher = fetcher
        self.now = now
        #: 单次采集 pass 的时间预算。`None` = 用模块默认（生产路径）；
        #: 传具体秒数 = 固定预算（测试与嵌入式用法走这条）。
        self.pass_budget_seconds = (
            COLLECT_PASS_BUDGET_SECONDS
            if pass_budget_seconds is None
            else float(pass_budget_seconds)
        )
        self.on_item_stored = on_item_stored

    def ensure_public_defaults(self):
        """Seed the verified preset sources and retire legacy ones in place."""
        defaults = (
            {"source_id": "S-HY-GOV-NEWS", "name": "河源市政府门户·要闻动态",
             "kind": "html_list", "endpoint": "http://www.heyuan.gov.cn/ywdt/index.html",
             "region": "heyuan", "category": "gov", "refresh_minutes": 60,
             "reliability_weight": 0.9, "tier": "T1"},
            {"source_id": "S-HY-GOV-PUB", "name": "河源市政府门户·政务公开",
             "kind": "html_list", "endpoint": "http://www.heyuan.gov.cn/zwgk/index.html",
             "region": "heyuan", "category": "gov", "refresh_minutes": 60,
             "reliability_weight": 0.9, "tier": "T1"},
            {"source_id": "S-HY-GGZYPZ", "name": "河源市公共资源配置信息",
             "kind": "html_list",
             "endpoint": "https://www.heyuan.gov.cn/zwgk/zdlyxx/ggzypz/",
             "region": "heyuan", "category": "procurement", "refresh_minutes": 30,
             "reliability_weight": 0.9, "tier": "T1"},
            {"source_id": "S-HY-RB", "name": "河源网（河源日报）",
             "kind": "html_list", "endpoint": "http://www.hyrbnews.cn/",
             "region": "heyuan", "category": "news", "refresh_minutes": 60,
             "reliability_weight": 0.85, "tier": "T2"},
            {"source_id": "S-HY-RTV", "name": "河源网络广播电视台",
             "kind": "html_list", "endpoint": "http://www.hyrtv.cn/",
             "region": "heyuan", "category": "news", "refresh_minutes": 60,
             "reliability_weight": 0.8, "tier": "T2"},
            {"source_id": "S-HY-XW", "name": "河源新闻网",
             "kind": "html_list", "endpoint": "http://www.heyuanxw.com/",
             "region": "heyuan", "category": "news", "refresh_minutes": 60,
             "reliability_weight": 0.8, "tier": "T2"},
            {"source_id": "S-GD-GOV", "name": "广东省人民政府门户",
             "kind": "html_list", "endpoint": "https://www.gd.gov.cn/",
             "region": "guangdong", "category": "gov", "refresh_minutes": 60,
             "reliability_weight": 0.9, "tier": "T1"},
            {"source_id": "S-GD-DRC", "name": "广东省发展和改革委员会",
             "kind": "html_list", "endpoint": "http://drc.gd.gov.cn/",
             "region": "guangdong", "category": "gov", "refresh_minutes": 60,
             "reliability_weight": 0.9, "tier": "T1"},
            {"source_id": "S-GD-SL", "name": "广东省水利厅",
             "kind": "html_list", "endpoint": "http://slt.gd.gov.cn/",
             "region": "guangdong", "category": "water", "refresh_minutes": 60,
             "reliability_weight": 0.9, "tier": "T1"},
            {"source_id": "S-GD-ZFCXJST", "name": "广东省住房和城乡建设厅",
             "kind": "html_list", "endpoint": "https://zfcxjst.gd.gov.cn/",
             "region": "guangdong", "category": "housing", "refresh_minutes": 60,
             "reliability_weight": 0.9, "tier": "T1"},
            {"source_id": "S-GD-SOUTH", "name": "南方网",
             "kind": "html_list", "endpoint": "https://www.southcn.com/",
             "region": "guangdong", "category": "news", "refresh_minutes": 60,
             "reliability_weight": 0.85, "tier": "T2"},
            {"source_id": "S-CN-PEOPLE-POL", "name": "人民网RSS·时政",
             "kind": "rss", "endpoint": "http://www.people.com.cn/rss/politics.xml",
             "region": "national", "category": "gov", "refresh_minutes": 60,
             "reliability_weight": 0.9, "tier": "T1"},
            {"source_id": "S-CN-PEOPLE-FIN", "name": "人民网RSS·财经",
             "kind": "rss", "endpoint": "http://www.people.com.cn/rss/finance.xml",
             "region": "national", "category": "finance", "refresh_minutes": 60,
             "reliability_weight": 0.9, "tier": "T1"},
            {"source_id": "S-CN-CCGP", "name": "中国政府采购网",
             "kind": "html_list", "endpoint": "http://www.ccgp.gov.cn/",
             "region": "national", "category": "procurement", "refresh_minutes": 30,
             "reliability_weight": 0.9, "tier": "T1"},
            {"source_id": "S-CN-XINHUA", "name": "新华网",
             "kind": "html_list", "endpoint": "http://www.xinhuanet.com/",
             "region": "national", "category": "finance", "refresh_minutes": 60,
             "reliability_weight": 0.85, "tier": "T1"},
            {"source_id": "S-GDELT-CHINA", "name": "GDELT全球新闻索引",
             "kind": "gdelt",
             "endpoint": "https://api.gdeltproject.org/api/v2/doc/doc?query=China&mode=artlist&format=json&maxrecords=25&timespan=1d",
             "region": "global", "category": "general",
             "refresh_minutes": 120,
             "reliability_weight": 0.65, "tier": "T3"},
            {"source_id": "S-YGP-HY", "name": "广东公共资源交易·河源全量公告",
             "kind": "json_api",
             "endpoint": "https://ygp.gdzwfw.gov.cn/ggzy-portal/search/v2/items",
             "region": "heyuan", "category": "procurement", "refresh_minutes": 120,
             "reliability_weight": 0.95, "tier": "T1", "config": {
                 "request_payload": {
                     "pageNo": 1, "pageSize": 50, "keyword": "",
                     "siteCode": "441600", "secondType": "", "tradingProcess": "",
                     "thirdType": "[]", "projectType": "",
                     "publishStartTime": "", "publishEndTime": "",
                     "type": "trading-type", "openConvert": False,
                 },
                 "items_path": "data.pageData",
                 "fields": {
                     "title": "noticeTitle", "published_at": "publishDate",
                     "summary": "noticeThirdTypeDesc",
                 },
                 "url_template": (
                     "https://ygp.gdzwfw.gov.cn/ggzy-portal/#/441600/jygg/"
                     "detail?noticeId={noticeId}"
                 ),
             }},
            {"source_id": "S-YGP-HY-D", "name": "广东公共资源交易·河源政府采购",
             "kind": "json_api",
             "endpoint": "https://ygp.gdzwfw.gov.cn/ggzy-portal/search/v2/items",
             "region": "heyuan", "category": "procurement", "refresh_minutes": 120,
             "reliability_weight": 0.95, "tier": "T1", "config": {
                 "request_payload": {
                     "pageNo": 1, "pageSize": 50, "keyword": "",
                     "siteCode": "441600", "secondType": "D", "tradingProcess": "",
                     "thirdType": "[]", "projectType": "",
                     "publishStartTime": "", "publishEndTime": "",
                     "type": "trading-type", "openConvert": False,
                 },
                 "items_path": "data.pageData",
                 "fields": {
                     "title": "noticeTitle", "published_at": "publishDate",
                     "summary": "noticeThirdTypeDesc",
                 },
                 "url_template": (
                     "https://ygp.gdzwfw.gov.cn/ggzy-portal/#/441600/jygg/"
                     "detail?noticeId={noticeId}"
                 ),
             }},
            # ── v8：全球态势图层（独立存储，不进 external_items / 聚类 / 研判 / 通知）──
            # 三家都是官方/半官方、无密钥、自带经纬度。category 用现有枚举里的
            # `general`（新增类目会牵动 5 处白名单，本轮取最小改动）；region=global。
            {"source_id": "S-USGS-QUAKE", "name": "USGS 全球地震（近一日）",
             "kind": "geojson",
             "endpoint": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson",
             "region": "global", "category": "general", "refresh_minutes": 20,
             "reliability_weight": 0.95, "tier": "T1", "config": {
                 "records_path": "features",
                 "layer": "quake",
                 "fields": {
                     "id": "id",
                     "title": "properties.title",
                     "summary": "properties.place",
                     "occurred_at": "properties.time",
                     "updated_at": "properties.updated",
                     "magnitude": "properties.mag",
                     "severity": "properties.alert",
                     "lat": "geometry.coordinates.1",
                     "lon": "geometry.coordinates.0",
                     "url": "properties.url",
                 },
             }},
            {"source_id": "S-NASA-EONET", "name": "NASA EONET 自然事件",
             "kind": "geojson",
             "endpoint": "https://eonet.gsfc.nasa.gov/api/v3/events?limit=300",
             "region": "global", "category": "general", "refresh_minutes": 30,
             "reliability_weight": 0.9, "tier": "T1", "config": {
                 "records_path": "events",
                 "layer_path": "categories.0.id",
                 "layer_map": {
                     "wildfires": "wildfire", "severeStorms": "storm",
                     "volcanoes": "volcano", "floods": "flood",
                     "drought": "drought", "earthquakes": "quake",
                 },
                 "default_layer": "disaster",
                 "fields": {
                     "id": "id",
                     "title": "title",
                     "summary": ["description", "link"],
                     "occurred_at": "geometry.0.date",
                     "magnitude": "geometry.0.magnitudeValue",
                     "lat": "geometry.0.coordinates.1",
                     "lon": "geometry.0.coordinates.0",
                     "url": ["sources.0.url", "link"],
                 },
             }},
            {"source_id": "S-GDACS", "name": "GDACS 全球灾害告警",
             "kind": "geojson",
             "endpoint": "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH",
             "region": "global", "category": "general", "refresh_minutes": 30,
             "reliability_weight": 0.9, "tier": "T1", "config": {
                 "records_path": "features",
                 "layer_path": "properties.eventtype",
                 "layer_map": {
                     "EQ": "quake", "TC": "storm", "FL": "flood",
                     "VO": "volcano", "WF": "wildfire", "DR": "drought",
                 },
                 "default_layer": "disaster",
                 "fields": {
                     "id": ["properties.eventid", "properties.episodeid"],
                     "title": "properties.name",
                     "summary": ["properties.description", "properties.htmldescription"],
                     "occurred_at": "properties.fromdate",
                     "updated_at": "properties.datemodified",
                     "magnitude": ["properties.severitydata.severity", "properties.alertscore"],
                     "severity": "properties.alertlevel",
                     "lat": "geometry.coordinates.1",
                     "lon": "geometry.coordinates.0",
                     "url": "properties.url.report",
                 },
             }},
        )
        legacy_ids = ("S-BBC-ZH", "S-MOHRSS-POLICY", "S-MFA-SAFETY")
        with self.database.connect() as connection:
            existing = {
                row[0]
                for row in connection.execute("SELECT source_id FROM external_sources")
            }
            # One-time retirement of legacy English defaults: disable in place,
            # keep history. Guarded by category so it is idempotent.
            connection.execute(
                """
                UPDATE external_sources SET enabled=0, category='legacy'
                WHERE source_id IN (?, ?, ?) AND user_managed=0
                  AND category != 'legacy'
                """,
                legacy_ids,
            )
        for source in defaults:
            if source["source_id"] not in existing:
                self.add_source({**source, "user_managed": 0})
            else:
                # 升级已存在的预置源，补上tier分级（不覆盖用户手动修改过的可靠度）
                tier = source.get("tier", "T3")
                with self.database.connect() as connection:
                    connection.execute(
                        "UPDATE external_sources SET tier=? WHERE source_id=? AND user_managed=0",
                        (tier, source["source_id"]),
                    )
                    # 预置源的抓取周期跟随代码默认值演进（2026-09-20：GDELT 15→120）。
                    # 只覆盖**显式声明了周期**的预置源，且只动 user_managed=0 的行 ——
                    # 用户自建/改过的源一律不碰。加 `!=` 条件是为了不做无意义的写入。
                    if "refresh_minutes" in source:
                        refresh = int(source["refresh_minutes"])
                        connection.execute(
                            "UPDATE external_sources SET refresh_minutes=?"
                            " WHERE source_id=? AND user_managed=0"
                            " AND refresh_minutes != ?",
                            (refresh, source["source_id"], refresh),
                        )

    def add_source(self, data):
        source_id = str(data.get("source_id") or f"S-{uuid.uuid4().hex[:12]}")
        name = str(data.get("name", "")).strip()
        kind = str(data.get("kind", "rss")).strip()
        endpoint = validate_public_url(
            data.get("endpoint") or data.get("url") or ""
        )
        refresh = int(data.get("refresh_minutes", 15))
        reliability = float(data.get("reliability_weight", 0.6))
        region = str(data.get("region", "global")).strip() or "global"
        category = str(data.get("category", "general")).strip() or "general"
        tier = str(data.get("tier", "T3")).strip().upper() or "T3"
        user_managed = 1 if int(data.get("user_managed", 1)) else 0
        if not name or kind not in SOURCE_KINDS:
            raise ValueError("数据源名称或类型无效")
        if region not in REGIONS or category not in SOURCE_CATEGORIES:
            raise ValueError("区域或类别无效")
        if tier not in SOURCE_TIERS:
            raise ValueError("信源分级无效（T1/T2/T3/T4）")
        # 未显式指定可靠度时，按分级设定默认值
        if "reliability_weight" not in data:
            reliability = TIER_RELIABILITY[tier]
        if refresh < 5 or refresh > 1440 or not 0 <= reliability <= 1:
            raise ValueError("刷新周期或可靠度无效")
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO external_sources(
                    source_id, name, kind, endpoint, enabled, refresh_minutes,
                    reliability_weight, config_json, next_fetch_at,
                    region, category, user_managed, tier
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    name,
                    kind,
                    endpoint,
                    1 if data.get("enabled", True) else 0,
                    refresh,
                    reliability,
                    json.dumps(data.get("config", {}), ensure_ascii=False),
                    iso(self.now()),
                    region,
                    category,
                    user_managed,
                    tier,
                ),
            )
        return source_id

    def update_source(self, source_id, data):
        """Partially update a source; only whitelisted fields are applied."""
        updates = {}
        if "name" in data or "source_name" in data:
            name = str(data.get("name") or data.get("source_name") or "").strip()
            if not name:
                raise ValueError("数据源名称无效")
            updates["name"] = name
        if "kind" in data:
            kind = str(data.get("kind", "")).strip()
            if kind not in SOURCE_KINDS:
                raise ValueError("数据源类型无效")
            updates["kind"] = kind
        if "endpoint" in data or "url" in data:
            updates["endpoint"] = validate_public_url(
                data.get("endpoint") or data.get("url") or ""
            )
        if "region" in data:
            region = str(data.get("region", "")).strip()
            if region not in REGIONS:
                raise ValueError("区域无效")
            updates["region"] = region
        if "category" in data:
            category = str(data.get("category", "")).strip()
            if category not in SOURCE_CATEGORIES:
                raise ValueError("类别无效")
            updates["category"] = category
        if "refresh_minutes" in data:
            refresh = int(data.get("refresh_minutes", 15))
            if not 5 <= refresh <= 1440:
                raise ValueError("刷新周期无效")
            updates["refresh_minutes"] = refresh
        if "reliability_weight" in data:
            reliability = float(data.get("reliability_weight", 0.6))
            if not 0 <= reliability <= 1:
                raise ValueError("可靠度无效")
            updates["reliability_weight"] = reliability
        if "tier" in data:
            tier = str(data.get("tier", "")).strip().upper()
            if tier not in SOURCE_TIERS:
                raise ValueError("信源分级无效（T1/T2/T3/T4）")
            updates["tier"] = tier
        if not updates:
            raise ValueError("没有可更新的字段")
        assignments = ", ".join(f"{key}=?" for key in updates)
        values = (*updates.values(), source_id)
        with self.database.connect() as connection:
            result = connection.execute(
                f"UPDATE external_sources SET {assignments} WHERE source_id = ?",
                values,
            )
            if result.rowcount != 1:
                raise KeyError(source_id)
        return {"source_id": source_id, "updated": sorted(updates)}

    def delete_source(self, source_id, purge_items=False):
        """Delete a user-managed source; preset sources can only be disabled."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT user_managed FROM external_sources WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            if row is None:
                raise KeyError(source_id)
            if not row["user_managed"]:
                raise ValueError("预置源不可删除，可改为停用")
            purged = 0
            if purge_items:
                items = [
                    row[0]
                    for row in connection.execute(
                        "SELECT item_id FROM external_items WHERE source_id = ?",
                        (source_id,),
                    )
                ]
                if items:
                    marks = ",".join("?" for _ in items)
                    connection.execute(
                        f"DELETE FROM external_matches WHERE item_id IN ({marks})",
                        items,
                    )
                    connection.execute(
                        "DELETE FROM event_cluster_items WHERE item_id IN "
                        f"({marks}) AND item_id NOT IN "
                        "(SELECT item_id FROM external_item_sources "
                        " WHERE source_id != ?)",
                        (*items, source_id),
                    )
                    connection.execute(
                        f"DELETE FROM external_item_sources WHERE item_id IN ({marks})",
                        items,
                    )
                    connection.execute(
                        f"DELETE FROM external_items WHERE item_id IN ({marks})",
                        items,
                    )
                    purged = len(items)
            connection.execute(
                "DELETE FROM external_sources WHERE source_id = ?", (source_id,)
            )
        return {
            "source_id": source_id,
            "deleted": True,
            "purged_items": purged,
        }

    def import_opml(self, data):
        """Import RSS sources from OPML text; unsafe URLs are skipped."""
        xml_text = str(data.get("xml") or data.get("opml_text") or "")
        path = str(data.get("opml_path") or "").strip()
        if not xml_text and path:
            if not path.lower().endswith(".opml"):
                raise ValueError("仅支持 .opml 文件")
            xml_text = Path(path).read_text(encoding="utf-8", errors="replace")
        if not xml_text.strip():
            raise ValueError("OPML内容为空")
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as error:
            raise ValueError(f"OPML解析失败：{error}") from error
        region = str(data.get("region", "global")).strip() or "global"
        category = str(data.get("category", "general")).strip() or "general"
        if region not in REGIONS or category not in SOURCE_CATEGORIES:
            raise ValueError("区域或类别无效")
        outlines = [
            element
            for element in root.iter()
            if element.tag.rsplit("}", 1)[-1] == "outline"
            and element.attrib.get("xmlUrl")
        ][:200]
        imported = duplicated = failed = 0
        with self.database.connect() as connection:
            existing = {
                row[0]
                for row in connection.execute(
                    "SELECT endpoint FROM external_sources"
                )
            }
        for outline in outlines:
            url = str(outline.attrib.get("xmlUrl", "")).strip()
            title = str(outline.attrib.get("title", "") or url).strip()[:120]
            try:
                endpoint = validate_public_url(url)
            except ValueError:
                failed += 1
                continue
            if endpoint in existing:
                duplicated += 1
                continue
            try:
                self.add_source(
                    {
                        "name": title,
                        "kind": "rss",
                        "endpoint": endpoint,
                        "region": region,
                        "category": category,
                        "refresh_minutes": 60,
                    }
                )
            except ValueError:
                failed += 1
                continue
            existing.add(endpoint)
            imported += 1
        return {"imported": imported, "duplicated": duplicated, "failed": failed}

    def add_watch_rule(self, data):
        query = " ".join(str(data.get("query", "")).split())
        importance = int(data.get("importance", 3))
        if not query or not 1 <= importance <= 5:
            raise ValueError("关注词或重要度无效")
        with self.database.connect() as connection:
            existing = next(
                (
                    row
                    for row in connection.execute(
                        "SELECT rule_id, query, importance FROM watch_rules"
                    )
                    if row["query"].casefold() == query.casefold()
                ),
                None,
            )
            if existing is not None:
                if importance > existing["importance"]:
                    connection.execute(
                        "UPDATE watch_rules SET importance = ? WHERE rule_id = ?",
                        (importance, existing["rule_id"]),
                    )
                return existing["rule_id"]
            rule_id = str(data.get("rule_id") or f"W-{uuid.uuid4().hex[:12]}")
            connection.execute(
                """
                INSERT INTO watch_rules(
                    rule_id, query, domains_json, interest_ids_json,
                    importance, enabled, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rule_id,
                    query,
                    json.dumps(data.get("domains", []), ensure_ascii=False),
                    json.dumps(data.get("interest_ids", []), ensure_ascii=False),
                    importance,
                    1 if data.get("enabled", True) else 0,
                    iso(self.now()),
                ),
            )
        return rule_id

    def list_rules(self):
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM watch_rules ORDER BY importance DESC, created_at"
            ).fetchall()
        return [
            {
                **dict(row),
                "domains": json.loads(row["domains_json"]),
                "interest_ids": json.loads(row["interest_ids_json"]),
                "enabled": bool(row["enabled"]),
            }
            for row in rows
        ]

    def set_rule_enabled(self, rule_id, enabled):
        """启用/停用一条关注词规则（UI 管理区复用）。"""
        if not isinstance(enabled, bool):
            raise ValueError("关注词状态无效")
        with self.database.connect() as connection:
            result = connection.execute(
                "UPDATE watch_rules SET enabled = ? WHERE rule_id = ?",
                (1 if enabled else 0, rule_id),
            )
            if result.rowcount == 0:
                raise KeyError(rule_id)
        return {"rule_id": rule_id, "enabled": enabled}

    def delete_watch_rule(self, rule_id):
        """删除一条关注词规则；历史命中记录保留。"""
        with self.database.connect() as connection:
            result = connection.execute(
                "DELETE FROM watch_rules WHERE rule_id = ?", (rule_id,)
            )
            if result.rowcount == 0:
                raise KeyError(rule_id)
        return {"rule_id": rule_id, "deleted": True}

    def list_sources(self, region=None, category=None, enabled=None):
        now = self.now()
        with self.database.connect() as connection:
            query = "SELECT * FROM external_sources"
            clauses = []
            values = []
            if region:
                clauses.append("region = ?")
                values.append(region)
            if category:
                clauses.append("category = ?")
                values.append(category)
            if enabled is not None:
                clauses.append("enabled = ?")
                values.append(1 if enabled else 0)
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY name"
            rows = connection.execute(query, values).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            last_success = parse_iso(row["last_success_at"])
            item["enabled"] = bool(row["enabled"])
            item["user_managed"] = bool(item.get("user_managed", 1))
            item["stale"] = bool(
                last_success
                and now - last_success > timedelta(minutes=row["refresh_minutes"] * 2)
            )
            failures = int(row["consecutive_failures"] or 0)
            status = row["last_status"] or "never"
            if status in {"never", ""}:
                health = "never"
            elif failures >= 5:
                health = "err"
            elif failures >= 2 or item["stale"]:
                health = "warn"
            else:
                health = "ok"
            item["health"] = health
            # Frontend contract: id/url aliases for source_id/endpoint.
            item["id"] = item["source_id"]
            item["url"] = item["endpoint"]
            output.append(item)
        return output

    def set_source_enabled(self, source_id, enabled):
        with self.database.connect() as connection:
            result = connection.execute(
                """
                UPDATE external_sources SET enabled = ?, next_fetch_at = CASE
                    WHEN ? = 1 THEN ? ELSE next_fetch_at END
                WHERE source_id = ?
                """,
                (1 if enabled else 0, 1 if enabled else 0, iso(self.now()), source_id),
            )
            if result.rowcount != 1:
                raise KeyError(source_id)
        return {"source_id": source_id, "enabled": bool(enabled)}

    def bulk_set_enabled(self, enabled, region=None, category=None):
        """Enable or disable every source matching the optional region/category
        filter. Returns the number of rows changed so the UI can confirm
        the action. The user can act on the entire catalogue or narrow by
        region (heyuan / guangdong / national / global) and/or category
        (gov / procurement / finance / news / water / housing / general).
        """
        clauses = []
        values: list = []
        if region:
            clauses.append("region = ?")
            values.append(region)
        if category:
            clauses.append("category = ?")
            values.append(category)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.database.connect() as connection:
            result = connection.execute(
                f"UPDATE external_sources SET enabled = ?{where}",
                (1 if enabled else 0, *values),
            )
            updated = int(result.rowcount or 0)
        return {
            "enabled": bool(enabled),
            "region": region or "",
            "category": category or "",
            "updated": updated,
        }

    def _source(self, source_id):
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM external_sources WHERE source_id = ?", (source_id,)
            ).fetchone()
        if row is None:
            raise KeyError(source_id)
        return dict(row)

    def _match(self, connection, item_id, title, summary, reliability):
        text = f"{title}\n{summary}".casefold()
        rules = connection.execute(
            "SELECT * FROM watch_rules WHERE enabled = 1"
        ).fetchall()
        for rule in rules:
            query = rule["query"].casefold()
            if query not in text:
                continue
            title_hit = query in title.casefold()
            score = min(1.0, 0.15 * rule["importance"] + 0.2 * reliability + (0.15 if title_hit else 0.05))
            alert = "L4" if score >= 0.9 else "L3" if score >= 0.7 else "L2" if score >= 0.5 else "L1"
            reasons = [f"命中关注词：{rule['query']}", f"关注重要度：{rule['importance']}/5"]
            connection.execute(
                """
                INSERT INTO external_matches(item_id, rule_id, score, reasons_json, alert_level)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(item_id, rule_id) DO UPDATE SET
                    score=excluded.score,
                    reasons_json=excluded.reasons_json,
                    alert_level=excluded.alert_level
                """,
                (item_id, rule["rule_id"], score, json.dumps(reasons, ensure_ascii=False), alert),
            )

    def _store_item(self, connection, source, item, fetched_at):
        canonical = canonicalize_url(item.url)
        item_id = "E-" + hashlib.sha256(canonical.encode()).hexdigest()[:24]
        title = plain_text(item.title, max_length=300)
        summary = plain_text(item.summary, max_length=2000)
        content_hash = hashlib.sha256(
            f"{title}\n{summary}".encode("utf-8")
        ).hexdigest()
        exists = connection.execute(
            "SELECT item_id FROM external_items WHERE canonical_url = ?", (canonical,)
        ).fetchone()
        new = exists is None
        if exists is not None:
            item_id = exists["item_id"]
            connection.execute(
                "UPDATE external_items SET last_seen_at = ?, fetched_at = ? WHERE item_id = ?",
                (fetched_at, fetched_at, item_id),
            )
        else:
            connection.execute(
                """
                INSERT INTO external_items(
                    item_id, canonical_url, title, summary, published_at, fetched_at,
                    source_id, source_name, language, content_hash, first_seen_at,
                    last_seen_at, source_count, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    item_id,
                    canonical,
                    title,
                    summary,
                    item.published_at or None,
                    fetched_at,
                    source["source_id"],
                    source["name"],
                    item.language,
                    content_hash,
                    fetched_at,
                    fetched_at,
                    json.dumps(item.raw or {}, ensure_ascii=False),
                ),
            )
        connection.execute(
            """
            INSERT OR IGNORE INTO external_item_sources(item_id, source_id, url, first_seen_at)
            VALUES (?, ?, ?, ?)
            """,
            (item_id, source["source_id"], item.url, fetched_at),
        )
        count = connection.execute(
            "SELECT COUNT(*) FROM external_item_sources WHERE item_id = ?", (item_id,)
        ).fetchone()[0]
        connection.execute(
            "UPDATE external_items SET source_count = ? WHERE item_id = ?", (count, item_id)
        )
        self._match(
            connection,
            item_id,
            title,
            summary,
            source["reliability_weight"],
        )
        return new, item_id

    def _notify_stored_items(self, item_ids):
        if self.on_item_stored is None:
            return
        for item_id in dict.fromkeys(item_ids):
            try:
                self.on_item_stored(item_id)
            except Exception as error:
                with self.database.connect() as connection:
                    connection.execute(
                        """
                        INSERT INTO audit_log(
                            occurred_at,action,object_type,object_id,details_json
                        ) VALUES (?, 'cognition.process_failed', 'external_item', ?, ?)
                        """,
                        (
                            iso(self.now()),
                            item_id,
                            json.dumps(
                                {
                                    "error_type": type(error).__name__,
                                    "message": str(error),
                                },
                                ensure_ascii=False,
                            ),
                        ),
                    )

    def refresh_source(self, source_id):
        source = self._source(source_id)
        started = self.now()
        run_id = "R-" + uuid.uuid4().hex
        try:
            items = self.fetcher(source)
            fetched_at = iso(self.now())
            new_count = 0
            stored_item_ids = []
            with self.database.connect() as connection:
                for item in items:
                    is_new, item_id = self._store_item(
                        connection, source, item, fetched_at
                    )
                    new_count += int(is_new)
                    stored_item_ids.append(item_id)
                next_fetch = iso(self.now() + timedelta(minutes=source["refresh_minutes"]))
                connection.execute(
                    """
                    UPDATE external_sources SET last_attempt_at=?, last_success_at=?,
                        last_status='ok', last_error='', consecutive_failures=0,
                        next_fetch_at=? WHERE source_id=?
                    """,
                    (fetched_at, fetched_at, next_fetch, source_id),
                )
                connection.execute(
                    "INSERT INTO external_runs VALUES (?, ?, ?, ?, 'ok', ?, ?, '', '')",
                    (run_id, source_id, iso(started), fetched_at, len(items), new_count),
                )
            self._notify_stored_items(stored_item_ids)
            return {"status": "ok", "fetched_count": len(items), "new_count": new_count}
        except FetchError as error:
            finished = self.now()
            failures = source["consecutive_failures"] + 1
            delay = _failure_backoff_minutes(failures)
            with self.database.connect() as connection:
                connection.execute(
                    """
                    UPDATE external_sources SET last_attempt_at=?, last_status='error',
                        last_error=?, consecutive_failures=?, next_fetch_at=? WHERE source_id=?
                    """,
                    (iso(finished), str(error), failures, iso(finished + timedelta(minutes=delay)), source_id),
                )
                connection.execute(
                    "INSERT INTO external_runs VALUES (?, ?, ?, ?, 'error', 0, 0, ?, ?)",
                    (run_id, source_id, iso(started), iso(finished), error.error_type, str(error)),
                )
            return {"status": "error", "error_type": error.error_type, "message": str(error)}

    def radar_page(self, limit=10, offset=0, query=""):
        limit = int(limit)
        offset = int(offset)
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("分页参数无效")
        query = plain_text(query, max_length=100)
        where = ""
        values = []
        if query:
            where = " WHERE e.title LIKE ? OR e.summary LIKE ?"
            like = f"%{query}%"
            values.extend((like, like))
        with self.database.connect() as connection:
            total = connection.execute(
                f"""
                SELECT COUNT(DISTINCT e.item_id)
                FROM external_items e
                JOIN external_matches m ON m.item_id=e.item_id
                {where}
                """,
                values,
            ).fetchone()[0]
            items = connection.execute(
                f"""
                SELECT e.*, MAX(m.score) AS best_score
                FROM external_items e
                JOIN external_matches m ON m.item_id = e.item_id
                {where}
                GROUP BY e.item_id
                ORDER BY best_score DESC, COALESCE(e.published_at, e.first_seen_at) DESC
                LIMIT ? OFFSET ?
                """,
                (*values, limit, offset),
            ).fetchall()
            output = []
            for item in items:
                matches = connection.execute(
                    """
                    SELECT m.score, m.reasons_json, m.alert_level, w.rule_id, w.query,
                           w.importance
                    FROM external_matches m JOIN watch_rules w ON w.rule_id=m.rule_id
                    WHERE m.item_id=? ORDER BY m.score DESC
                    """,
                    (item["item_id"],),
                ).fetchall()
                best_alert = max(
                    (match["alert_level"] for match in matches),
                    key=lambda value: int(value[1:]),
                )
                row = dict(item)
                row["title"] = plain_text(row.get("title"), max_length=300)
                row["summary"] = plain_text(row.get("summary"), max_length=2000)
                row["alert_level"] = best_alert
                row["matched_rules"] = [
                    {
                        "rule_id": match["rule_id"],
                        "query": match["query"],
                        "importance": match["importance"],
                        "score": match["score"],
                        "reasons": json.loads(match["reasons_json"]),
                    }
                    for match in matches
                ]
                output.append(row)
        return {"items": output, "total": total, "limit": limit, "offset": offset}

    def radar_items(self, limit=100):
        safe_limit = max(1, min(int(limit), 100))
        return self.radar_page(limit=safe_limit)["items"]

    def refresh_due_sources(self):
        """抓取到期源，**单次调用不超过 `pass_budget_seconds`**；返回本轮真正抓了几个源。

        返回值的含义是**本轮真正发起抓取的源数**，不是"到期源数"：两者在预算到点
        时会不同，而任务状态里那个数字会被人读着判断"采集正常吗" —— 写"到期 35 个"
        而实际只抓了 12 个，就是又一处安静的说谎。差额由下一步的日志如实说出。

        **到点只停不丢**（这是"不许把源饿死"的全部依据）：

        - 未处理的源 `next_fetch_at` **原样不动** —— 它本来就已经到期，
          下一轮必然还在到期集合里；
        - 已处理的源由 `refresh_source` 自己把 `next_fetch_at` 推到将来
          （失败则按既有退避序列推到将来），于是它自然退出到期集合；
        - 集合按 `source_id` 排序，所以每一轮都从"最旧的未处理"接着抓。

        三条合起来：**每个源最终都会被抓到**，只是分摊到多轮；而任何一轮都不会
        长时间独占调度循环。

        预算约束的是**开新抓取的决策点**，不是"掐断正在途中的那次抓取"——
        单次 HTTP 抓取没法从外面中断（那要另加线程/超时机制，本批次明确不做）。
        所以：`已用 ≥ 预算` 时不再开新的，但**第一个源永远会开**（保证每一轮都有
        进展，预算再小也不会陷入"永远什么都没抓"）。因此一轮的实际耗时上界是
        `预算 + 单源上界`，不是恰好等于预算。
        """
        due_at = iso(self.now())
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT source_id FROM external_sources
                WHERE enabled = 1 AND kind != ?
                  AND (next_fetch_at IS NULL OR next_fetch_at <= ?)
                ORDER BY source_id
                """,
                (SITUATION_SOURCE_KIND, due_at),
            ).fetchall()
        budget = float(self.pass_budget_seconds)
        deadline = time.monotonic() + budget
        attempted = 0
        for row in rows:
            # 先看表再动手：反过来（干完再看）会让每一轮都超出一整个源的耗时，
            # 预算就不成其为预算了。`attempted` 那个条件是"至少开一次"的保证。
            if attempted and time.monotonic() >= deadline:
                break
            self.refresh_source(row["source_id"])
            attempted += 1
        deferred = len(rows) - attempted
        if deferred:
            _logger.info(
                "本轮采集到点（预算 %.0f 秒）：已抓 %d 个源，剩余 %d 个到期源留到下一轮",
                budget,
                attempted,
                deferred,
            )
        return attempted

    # ── 全球态势图层（v8）：独立抓取路径 + 独立只读查询 ──────────────────
    def refresh_situation_layers(self):
        """抓取到期的态势源，写入 `situation_events`。

        **不走 `refresh_due_sources`**：那条路会写 `external_items`，进而进聚类/研判/
        通知。这里只回一个"本轮抓了几个源"的计数，单源失败不抛、不影响其他源。
        """
        due_at = iso(self.now())
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT source_id FROM external_sources
                WHERE enabled = 1 AND kind = ?
                  AND (next_fetch_at IS NULL OR next_fetch_at <= ?)
                ORDER BY source_id
                """,
                (SITUATION_SOURCE_KIND, due_at),
            ).fetchall()
        for row in rows:
            self.refresh_situation_source(row["source_id"])
        return len(rows)

    def _situation_event_id(self, source_id, point):
        """稳定 id：同一事件重复抓取必须得到同一个 `G-…`。

        上游自带稳定标识（USGS 的 id / EONET 的 id / GDACS 的 eventid+episodeid）时用
        它；缺失时退回"标题+坐标+发生时刻"的哈希 —— 仍比随机 uuid 稳定。
        加 `source_id` 前缀是刻意的：态势层暂不做跨源同一事件合并，
        前缀保证不同源的同名事件不会互相覆盖。
        """
        key = point.event_key or f"{point.title}|{point.lat}|{point.lon}|{point.occurred_at}"
        digest = hashlib.sha256(f"{source_id}:{key}".encode("utf-8")).hexdigest()
        return "G-" + digest[:24]

    def _store_situation_points(self, connection, source, points, fetched_at):
        """按 `event_id` 幂等 upsert：保留 `first_seen_at`，刷新 `last_seen_at`。"""
        new = updated = 0
        for point in points:
            event_id = self._situation_event_id(source["source_id"], point)
            # 外部文本一律清洗后再落库（与 external_items 同一条纪律）：GDACS 的
            # htmldescription 之类带标记的字段若原样存下，Phase 2 的地图弹窗就会
            # 渲染出外部 HTML。
            title = plain_text(point.title, max_length=300)
            summary = plain_text(point.summary, max_length=2000)
            exists = connection.execute(
                "SELECT 1 FROM situation_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            updated_at = point.updated_at or fetched_at
            if exists is None:
                connection.execute(
                    """
                    INSERT INTO situation_events(
                        event_id, layer, title, summary, lat, lon, magnitude,
                        severity, occurred_at, updated_at, source_id, source_name,
                        canonical_url, raw_json, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        point.layer,
                        title,
                        summary,
                        point.lat,
                        point.lon,
                        point.magnitude,
                        point.severity,
                        point.occurred_at,
                        updated_at,
                        source["source_id"],
                        source["name"],
                        point.url,
                        json.dumps(point.raw or {}, ensure_ascii=False),
                        fetched_at,
                        fetched_at,
                    ),
                )
                new += 1
            else:
                connection.execute(
                    """
                    UPDATE situation_events SET
                        layer=?, title=?, summary=?, lat=?, lon=?, magnitude=?,
                        severity=?, occurred_at=?, updated_at=?, source_name=?,
                        canonical_url=?, raw_json=?, last_seen_at=?
                    WHERE event_id=?
                    """,
                    (
                        point.layer,
                        title,
                        summary,
                        point.lat,
                        point.lon,
                        point.magnitude,
                        point.severity,
                        point.occurred_at,
                        updated_at,
                        source["name"],
                        point.url,
                        json.dumps(point.raw or {}, ensure_ascii=False),
                        fetched_at,
                        event_id,
                    ),
                )
                updated += 1
        return new, updated

    def refresh_situation_source(self, source_id):
        """抓一个态势源：成功写 situation_events，失败只记状态（与 refresh_source 同款）。"""
        source = self._source(source_id)
        started = self.now()
        run_id = "R-" + uuid.uuid4().hex
        try:
            points = self.fetcher(source)
            fetched_at = iso(self.now())
            with self.database.connect() as connection:
                new_count, updated_count = self._store_situation_points(
                    connection, source, points, fetched_at
                )
                next_fetch = iso(
                    self.now() + timedelta(minutes=source["refresh_minutes"])
                )
                connection.execute(
                    """
                    UPDATE external_sources SET last_attempt_at=?, last_success_at=?,
                        last_status='ok', last_error='', consecutive_failures=0,
                        next_fetch_at=? WHERE source_id=?
                    """,
                    (fetched_at, fetched_at, next_fetch, source_id),
                )
                connection.execute(
                    "INSERT INTO external_runs VALUES (?, ?, ?, ?, 'ok', ?, ?, '', '')",
                    (run_id, source_id, iso(started), fetched_at, len(points), new_count),
                )
            return {
                "status": "ok",
                "fetched_count": len(points),
                "new_count": new_count,
                "updated_count": updated_count,
            }
        except FetchError as error:
            finished = self.now()
            failures = source["consecutive_failures"] + 1
            delay = _failure_backoff_minutes(failures)
            with self.database.connect() as connection:
                connection.execute(
                    """
                    UPDATE external_sources SET last_attempt_at=?, last_status='error',
                        last_error=?, consecutive_failures=?, next_fetch_at=? WHERE source_id=?
                    """,
                    (
                        iso(finished),
                        str(error),
                        failures,
                        iso(finished + timedelta(minutes=delay)),
                        source_id,
                    ),
                )
                connection.execute(
                    "INSERT INTO external_runs VALUES (?, ?, ?, ?, 'error', 0, 0, ?, ?)",
                    (run_id, source_id, iso(started), iso(finished), error.error_type, str(error)),
                )
            return {
                "status": "error",
                "error_type": error.error_type,
                "message": str(error),
            }

    def situation_layers(self):
        """每图层的条数、最近发生时间，以及各态势源的抓取状态。只读，不触发抓取。"""
        with self.database.connect() as connection:
            layer_rows = connection.execute(
                """
                SELECT layer, COUNT(*) AS count, MAX(occurred_at) AS latest_occurred_at,
                       GROUP_CONCAT(DISTINCT source_id) AS source_ids
                FROM situation_events
                GROUP BY layer
                ORDER BY count DESC, layer
                """
            ).fetchall()
            source_rows = connection.execute(
                """
                SELECT source_id, name, last_status, last_attempt_at, last_success_at,
                       last_error, consecutive_failures, next_fetch_at
                FROM external_sources WHERE kind = ? ORDER BY source_id
                """,
                (SITUATION_SOURCE_KIND,),
            ).fetchall()
        layers = [
            {
                "layer": row["layer"],
                "name": SITUATION_LAYER_NAMES.get(row["layer"], row["layer"]),
                "count": int(row["count"]),
                "latest_occurred_at": row["latest_occurred_at"],
                "source_ids": (row["source_ids"] or "").split(",") if row["source_ids"] else [],
            }
            for row in layer_rows
        ]
        return {"layers": layers, "sources": [dict(row) for row in source_rows]}

    def situation_points(self, *, layer="", hours=24, bbox="", limit=500):
        """按图层 / 时间窗 / 包围盒取点。**只读，绝不触发外网抓取。**"""
        hours = int(hours)
        limit = int(limit)
        if not 1 <= hours <= 168:
            raise ValueError("hours 需在 1-168 之间")
        if not 1 <= limit <= 2000:
            raise ValueError("limit 需在 1-2000 之间")
        layer = str(layer or "").strip()
        if layer and layer not in SITUATION_LAYER_NAMES:
            raise ValueError("图层无效")
        box = self._parse_bbox(bbox)
        since = iso(self.now() - timedelta(hours=hours))
        clauses = ["occurred_at IS NOT NULL", "occurred_at != ''", "occurred_at >= ?"]
        values: list = [since]
        if layer:
            clauses.append("layer = ?")
            values.append(layer)
        if box is not None:
            min_lon, min_lat, max_lon, max_lat = box
            clauses.append("lon BETWEEN ? AND ?")
            clauses.append("lat BETWEEN ? AND ?")
            values.extend((min_lon, max_lon, min_lat, max_lat))
        where = " WHERE " + " AND ".join(clauses)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT event_id, layer, title, lat, lon, magnitude, severity,
                       occurred_at, source_name, canonical_url
                FROM situation_events{where}
                ORDER BY occurred_at DESC, event_id
                LIMIT ?
                """,
                (*values, limit),
            ).fetchall()
        points = [
            {
                "event_id": row["event_id"],
                "layer": row["layer"],
                "layer_name": SITUATION_LAYER_NAMES.get(row["layer"], row["layer"]),
                "title": row["title"],
                "lat": row["lat"],
                "lon": row["lon"],
                "magnitude": row["magnitude"],
                "severity": row["severity"],
                "occurred_at": row["occurred_at"],
                "source_name": row["source_name"],
                "canonical_url": row["canonical_url"],
            }
            for row in rows
        ]
        return {"points": points, "count": len(points), "hours": hours, "limit": limit}

    @staticmethod
    def _parse_bbox(bbox):
        text = str(bbox or "").strip()
        if not text:
            return None
        parts = [piece.strip() for piece in text.split(",")]
        if len(parts) != 4:
            raise ValueError("bbox 需为 min_lon,min_lat,max_lon,max_lat")
        try:
            min_lon, min_lat, max_lon, max_lat = (float(piece) for piece in parts)
        except ValueError as error:
            raise ValueError("bbox 需为四个数值") from error
        if not (-180 <= min_lon <= 180 and -180 <= max_lon <= 180):
            raise ValueError("bbox 经度超出范围")
        if not (-90 <= min_lat <= 90 and -90 <= max_lat <= 90):
            raise ValueError("bbox 纬度超出范围")
        if min_lon > max_lon or min_lat > max_lat:
            raise ValueError("bbox 的最小值必须不大于最大值")
        return min_lon, min_lat, max_lon, max_lat
