import hashlib
import json
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
    parse_html_list,
    parse_json_api,
    validate_public_url,
)
from .text_cleaning import plain_text


REGIONS = {"heyuan", "guangdong", "national", "global"}
SOURCE_CATEGORIES = {
    "gov", "water", "housing", "procurement", "industry", "news",
    "finance", "general", "legacy",
}
SOURCE_KINDS = {"rss", "gdelt", "html_list", "json_api"}
# P4: 信源分级——T1官方源 / T2权威媒体 / T3聚合或一般 / T4未验证
SOURCE_TIERS = {"T1", "T2", "T3", "T4"}
TIER_RELIABILITY = {"T1": 0.9, "T2": 0.7, "T3": 0.5, "T4": 0.3}


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
        self, database, fetcher=fetch_source, now=utc_now, on_item_stored=None
    ):
        self.database = database
        self.fetcher = fetcher
        self.now = now
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
            delay = min(60, 15 * (2 ** (failures - 1)))
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
        due_at = iso(self.now())
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT source_id FROM external_sources
                WHERE enabled = 1 AND (next_fetch_at IS NULL OR next_fetch_at <= ?)
                ORDER BY source_id
                """,
                (due_at,),
            ).fetchall()
        for row in rows:
            self.refresh_source(row["source_id"])
        return len(rows)
