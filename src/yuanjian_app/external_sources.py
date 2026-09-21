import ipaddress
import json
import re
import socket
import ssl
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse


DEFAULT_TIMEOUT = 10
DEFAULT_MAX_BYTES = 5 * 1024 * 1024


class FetchError(RuntimeError):
    def __init__(self, error_type, message):
        super().__init__(message)
        self.error_type = error_type


@dataclass(frozen=True)
class ExternalItem:
    source_id: str
    source_name: str
    url: str
    title: str
    summary: str = ""
    published_at: str = ""
    language: str = ""
    raw: dict | None = None


@dataclass(frozen=True)
class SituationPoint:
    """全球态势图层的一个点事件（独立于 `ExternalItem`）。

    刻意**不复用** `ExternalItem`：那一个类型会被写进 `external_items`、进而进聚类与
    AI 研判。态势层是"加一层"，必须与那条链在**类型层面**就隔开 —— 一旦误传，
    `_store_item` 会因为缺 `.url` 之类的字段当场报错，而不是悄悄把地震灌进研判。
    """

    event_key: str
    layer: str
    title: str
    lat: float
    lon: float
    summary: str = ""
    magnitude: float | None = None
    severity: str = ""
    occurred_at: str = ""
    updated_at: str = ""
    url: str = ""
    raw: dict | None = None


def normalize_published_at(value):
    """Normalize ISO, RFC 2822, GDELT and compact 14-digit timestamps."""
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = None
    try:
        if len(text) == 16 and text.endswith("Z") and text[8] == "T":
            parsed = datetime.strptime(text, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc
            )
        elif len(text) == 14 and text.isdigit():
            # 广东公共资源交易平台 publishDate 形如 20260815093000
            parsed = datetime.strptime(text, "%Y%m%d%H%M%S").replace(
                tzinfo=timezone.utc
            )
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _local_address(address):
    value = ipaddress.ip_address(address)
    return not value.is_global


def validate_public_url(url):
    parsed = urlparse(str(url).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("只允许HTTP或HTTPS公网地址")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("不允许访问本机或局域网地址")
    try:
        if _local_address(host):
            raise ValueError("不允许访问本机或私网地址")
    except ValueError as error:
        if "不允许" in str(error):
            raise
    return parsed.geturl()


def _validate_dns(hostname, resolver):
    try:
        addresses = {row[4][0] for row in resolver(hostname, None, type=socket.SOCK_STREAM)}
    except (OSError, socket.gaierror) as error:
        raise FetchError("unreachable", f"域名无法解析：{error}") from error
    if not addresses or any(_local_address(address) for address in addresses):
        raise FetchError("unsafe_url", "域名解析到了非公网地址")


def fetch_bytes(
    url,
    *,
    opener=urllib.request.urlopen,
    timeout=DEFAULT_TIMEOUT,
    max_bytes=DEFAULT_MAX_BYTES,
    resolver=socket.getaddrinfo,
):
    safe_url = validate_public_url(url)
    _validate_dns(urlparse(safe_url).hostname, resolver)
    request = urllib.request.Request(
        safe_url,
        headers={"User-Agent": "YuanJian-Cognition/1.0 (+local personal research)"},
    )
    try:
        with opener(request, timeout=timeout) as response:
            body = response.read(max_bytes + 1)
    except (TimeoutError, socket.timeout) as error:
        raise FetchError("timeout", "外部源请求超时") from error
    except urllib.error.HTTPError as error:
        kind = "rate_limited" if error.code == 429 else "http_error"
        raise FetchError(kind, f"外部源返回HTTP {error.code}") from error
    except (urllib.error.URLError, OSError) as error:
        # urllib 把 SSL 错误包装在 URLError.reason 中；政府网站常见证书问题，降级为不验证
        reason = getattr(error, "reason", error)
        if isinstance(reason, (ssl.SSLError, ssl.SSLCertVerificationError)):
            try:
                unverified_ctx = ssl.create_default_context()
                unverified_ctx.check_hostname = False
                unverified_ctx.verify_mode = ssl.CERT_NONE
                with opener(request, timeout=timeout, context=unverified_ctx) as response:
                    body = response.read(max_bytes + 1)
            except (TimeoutError, socket.timeout) as inner:
                raise FetchError("timeout", "外部源请求超时") from inner
            except urllib.error.HTTPError as inner:
                kind = "rate_limited" if inner.code == 429 else "http_error"
                raise FetchError(kind, f"外部源返回HTTP {inner.code}") from inner
            except (urllib.error.URLError, OSError) as inner:
                raise FetchError("unreachable", f"外部源不可达：{inner}") from inner
        else:
            raise FetchError("unreachable", f"外部源不可达：{error}") from error
    if len(body) > max_bytes:
        raise FetchError("too_large", f"响应超过{max_bytes}字节上限")
    return body


def fetch_json(
    url,
    payload,
    *,
    opener=urllib.request.urlopen,
    timeout=DEFAULT_TIMEOUT,
    max_bytes=DEFAULT_MAX_BYTES,
    resolver=socket.getaddrinfo,
):
    """POST a JSON query to a public API endpoint with the same safety net."""
    safe_url = validate_public_url(url)
    _validate_dns(urlparse(safe_url).hostname, resolver)
    body = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(
        safe_url,
        data=body,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) YuanJian/1.0",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with opener(request, timeout=timeout) as response:
            data = response.read(max_bytes + 1)
    except (TimeoutError, socket.timeout) as error:
        raise FetchError("timeout", "外部源请求超时") from error
    except urllib.error.HTTPError as error:
        kind = "rate_limited" if error.code == 429 else "http_error"
        raise FetchError(kind, f"外部源返回HTTP {error.code}") from error
    except (urllib.error.URLError, OSError) as error:
        raise FetchError("unreachable", f"外部源不可达：{error}") from error
    if len(data) > max_bytes:
        raise FetchError("too_large", f"响应超过{max_bytes}字节上限")
    return data


def parse_json_api(body, source_id, source_name, config):
    """Parse items from a public JSON API driven by source config_json.

    Config keys: items_path ("data.pageData"), fields {title, url,
    published_at, summary, language}, url_template with {field} placeholders.
    """
    config = config or {}
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FetchError("parse_error", f"JSON接口解析失败：{error}") from error
    items = payload
    for key in str(config.get("items_path", "")).split("."):
        if not key:
            continue
        if not isinstance(items, dict) or key not in items:
            raise FetchError("parse_error", f"JSON路径无效：{config.get('items_path')}")
        items = items[key]
    if not isinstance(items, list):
        raise FetchError("parse_error", "JSON路径未指向列表")
    fields = config.get("fields") or {}
    template = str(config.get("url_template") or "")
    output = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get(fields.get("title", "title"), "") or "").strip()
        if template:
            try:
                url = template.format(**raw)
            except (KeyError, IndexError):
                url = ""
        else:
            url = str(raw.get(fields.get("url", "url"), "") or "").strip()
        if not title or not url:
            continue
        output.append(
            ExternalItem(
                source_id=source_id,
                source_name=source_name,
                url=url,
                title=title,
                summary=str(raw.get(fields.get("summary", "summary"), "") or ""),
                published_at=normalize_published_at(
                    raw.get(fields.get("published_at", "publishDate"), "")
                ),
                language="",
                raw=raw,
            )
        )
    return output


def _pluck(root, path):
    """通用取值器：点号路径 + 下标，支持 `*` 通配。

    例：
        `features.*.properties.title` → 每个 feature 的标题列表
        `geometry.coordinates.0`      → 坐标数组的第 0 个（经度）
        `categories.0.id`             → 首个类目 id

    取不到一律返回 `None`（不抛），调用方据此跳过该记录 —— 三个源的结构差异
    全部压进 `config_json`，解析器里**不硬编码任何源**。
    """
    if path in (None, ""):
        return root
    tokens = str(path).split(".")
    current = root
    for index, token in enumerate(tokens):
        if token == "*":
            if not isinstance(current, list):
                return None
            rest = ".".join(tokens[index + 1 :])
            return [_pluck(item, rest) for item in current]
        if isinstance(current, list):
            if not token.isdigit():
                return None
            position = int(token)
            if position >= len(current):
                return None
            current = current[position]
        elif isinstance(current, dict):
            if token not in current:
                return None
            current = current[token]
        else:
            return None
    return current


def _spec_values(root, spec):
    if isinstance(spec, (list, tuple)):
        return [_pluck(root, item) for item in spec]
    return [_pluck(root, spec)]


def _as_text(value):
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()


def _first_text(root, spec):
    """按顺序取第一个非空文本（用于 url/summary 这类"有首选、有兜底"的字段）。"""
    for value in _spec_values(root, spec):
        text = _as_text(value)
        if text:
            return text
    return ""


def _join_text(root, spec):
    """把多个路径拼成一个稳定标识（GDACS 的 eventid + episodeid）。"""
    return "-".join(
        text for text in (_as_text(value) for value in _spec_values(root, spec)) if text
    )


def _pluck_number(root, spec):
    for value in _spec_values(root, spec):
        if isinstance(value, bool) or value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _epoch_to_iso(number):
    value = float(number)
    if value > 1e11:  # 毫秒（USGS properties.time 形如 1789918095590）
        value /= 1000.0
    try:
        moment = datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return ""
    # 统一到秒：ISO 字符串要能直接做字典序比较（范围过滤与排序都靠它）。
    # 若 USGS 留 6 位微秒、GDACS 留 0 位，同一秒内会因 '.'(0x2E) < 'Z'(0x5A)
    # 而排错序 —— 地图上到秒足够，统一抹掉微秒。
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_situation_time(value):
    """态势层时间归一：epoch 秒/毫秒、ISO、RFC2822 都收敛成 UTC ISO。"""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return _epoch_to_iso(value)
    text = str(value).strip()
    if not text:
        return ""
    if text.isdigit() and len(text) >= 9:
        return _epoch_to_iso(int(text))
    return normalize_published_at(text)


def parse_geojson(body, source_id, source_name, config):
    """按 `config_json` 驱动解析 GeoJSON / JSON 点事件列表。

    与 `parse_json_api` / `parse_gdelt` 同款：一个解析器 + 一份配置，源差异不入代码。
    `config_json` 形如：
        {"records_path": "features", "layer": "quake",
         "fields": {"id": "id", "title": "properties.title",
                    "lat": "geometry.coordinates.1", "lon": "geometry.coordinates.0",
                    "occurred_at": "properties.time", "magnitude": "properties.mag",
                    "severity": "properties.alert", "url": "properties.url"}}
    或按类别派生图层：
        {"records_path": "events", "layer_path": "categories.0.id",
         "layer_map": {"wildfires": "wildfire"}, "default_layer": "disaster",
         "fields": {"id": "id", "lat": "geometry.0.coordinates.1", ...}}

    缺坐标、坐标非数值或越界、缺标题的记录一律**跳过**（不抛）—— 单条脏数据不该
    让整轮抓取失败。
    """
    config = config or {}
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FetchError("parse_error", f"GeoJSON解析失败：{error}") from error
    records = _pluck(payload, config.get("records_path", ""))
    if records is None:
        records = []
    if not isinstance(records, list):
        raise FetchError("parse_error", "GeoJSON路径未指向列表")
    fields = config.get("fields") or {}
    constant_layer = _as_text(config.get("layer"))
    layer_path = _as_text(config.get("layer_path"))
    layer_map = config.get("layer_map") or {}
    default_layer = _as_text(config.get("default_layer")) or constant_layer or "other"
    points = []
    for raw in records:
        if not isinstance(raw, dict):
            continue
        lat = _pluck_number(raw, fields.get("lat"))
        lon = _pluck_number(raw, fields.get("lon"))
        if lat is None or lon is None:
            continue
        if not -90 <= lat <= 90 or not -180 <= lon <= 180:
            continue
        title = _first_text(raw, fields.get("title"))
        if not title:
            continue
        layer = constant_layer
        if not layer and layer_path:
            layer = _as_text(layer_map.get(_first_text(raw, layer_path), ""))
        if not layer:
            layer = default_layer
        points.append(
            SituationPoint(
                event_key=_join_text(raw, fields.get("id")),
                layer=layer or "other",
                title=title,
                lat=lat,
                lon=lon,
                summary=_first_text(raw, fields.get("summary")),
                magnitude=_pluck_number(raw, fields.get("magnitude")),
                severity=_first_text(raw, fields.get("severity")),
                occurred_at=normalize_situation_time(
                    _pluck(raw, fields.get("occurred_at"))
                ),
                updated_at=normalize_situation_time(
                    _pluck(raw, fields.get("updated_at"))
                ),
                url=_first_text(raw, fields.get("url")),
                raw=raw,
            )
        )
    return points


def _text(element, child_name):
    for child in element:
        if child.tag.rsplit("}", 1)[-1] == child_name:
            return " ".join("".join(child.itertext()).split())
    return ""


def _link(element):
    for child in element:
        if child.tag.rsplit("}", 1)[-1] != "link":
            continue
        href = child.attrib.get("href", "").strip()
        if href:
            return href
        if child.text:
            return child.text.strip()
    return ""


def _sanitize_xml_bytes(body):
    """清理 RSS/Atom 中常见的非法 XML：移除控制字符、转义未转义的 &。"""
    text = body.decode("utf-8", errors="replace")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    text = re.sub(r"&(?!(amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", text)
    return text.encode("utf-8")


def parse_feed(body, source_id, source_name, endpoint=""):
    # 防御 XML 炸弹：限制实体展开文本总量（Python 3.10+），
    # 避免恶意 RSS 用 billion laughs 攻击耗尽内存。
    parser = ET.XMLParser()
    try:
        parser.entity_expansion_text_limit = 100_000
    except AttributeError:
        pass
    try:
        root = ET.fromstring(body, parser=parser)
    except ET.ParseError:
        # 部分政府网站 RSS 含非法字符或未转义 &，容错清理后重试
        cleaned = _sanitize_xml_bytes(body)
        try:
            root = ET.fromstring(cleaned, parser=parser)
        except ET.ParseError as error:
            raise FetchError("parse_error", f"RSS/Atom解析失败：{error}") from error
    items = []
    for element in root.iter():
        kind = element.tag.rsplit("}", 1)[-1]
        if kind not in {"item", "entry"}:
            continue
        title = unescape(_text(element, "title")).strip()
        url = _link(element)
        if not title or not url:
            continue
        summary = _text(element, "description") or _text(element, "summary") or _text(element, "content")
        published = _text(element, "pubDate") or _text(element, "published") or _text(element, "updated")
        items.append(
            ExternalItem(
                source_id=source_id,
                source_name=source_name,
                url=url,
                title=title,
                summary=unescape(summary),
                published_at=normalize_published_at(published),
                raw={"endpoint": endpoint},
            )
        )
    return items


def parse_gdelt(body, source_id, source_name):
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FetchError("parse_error", f"GDELT JSON解析失败：{error}") from error
    items = []
    for article in payload.get("articles", []):
        title = str(article.get("title", "")).strip()
        url = str(article.get("url", "")).strip()
        if not title or not url:
            continue
        provenance = " · ".join(
            value
            for value in (
                str(article.get("domain", "")).strip(),
                str(article.get("sourcecountry", "")).strip(),
            )
            if value
        )
        items.append(
            ExternalItem(
                source_id=source_id,
                source_name=source_name,
                url=url,
                title=title,
                summary=provenance,
                published_at=normalize_published_at(article.get("seendate", "")),
                language=str(article.get("language", "")),
                raw=article,
            )
        )
    return items


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.current_href = ""
        self.current_text = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self.current_href = dict(attrs).get("href", "").strip()
            self.current_text = []

    def handle_data(self, data):
        if self.current_href:
            self.current_text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self.current_href:
            self.links.append((self.current_href, " ".join("".join(self.current_text).split())))
            self.current_href = ""
            self.current_text = []


def _decode_document(body):
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return body.decode(encoding)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", errors="replace")


# html_list 噪声过滤：排除导航/页脚/栏目页等非文章链接
_NAV_TITLE_PATTERNS = re.compile(
    r"^(首页|网站首页|关于我们|联系方式|联系我们|网站地图|友情链接|版权所有|备案号|"
    r"粤ICP|京ICP|沪ICP|浙ICP|苏ICP|鲁ICP|川ICP|豫ICP|鄂ICP|湘ICP|闽ICP|"
    r"政务公开|政务服务|互动交流|信息公开|数据开放|无障碍|长者版|简体|繁体|English|"
    r"登录|注册|个人中心|退出|帮助中心|常见问题|意见反馈|在线访谈|调查征集|"
    r"广东省.*厅|广东省.*局|广东省.*委|广东省.*办|广东省.*中心|"
    r"河源市.*局|河源市.*委|河源市.*办|河源市.*中心|"
    r"市政府|市委|市人大|市政协|市纪委|组织部|宣传部|统战部|政法委|"
    r"省政府|省委|省人大|省政协|省纪委|"
    r"更多|更多>>|更多+|查看更多|more|More)$",
    re.IGNORECASE,
)
# 文章详情页 URL 特征：包含日期路径、文章ID、或常见文章关键词
_ARTICLE_URL_PATTERNS = re.compile(
    r"(/\d{4}/\d{2,4}/|/\d{6,}/|/\d{8,}/|/article/|/content/|/detail/|/info/|/notice/|"
    r"/\d+\.html?|/[a-z]+/\d+\.html?|/[a-z]+/\d+$|/ztzl/|/t\d{8}_)",
    re.IGNORECASE,
)
# 栏目/索引页 URL 特征：这些通常不是文章详情页
_SECTION_URL_PATTERNS = re.compile(
    r"(/index\.html?|/index$|/$|/list\.html?|/default\.html?|/default$|"
    r"/zwgk/|/ywdt/|/xxgk/|/gkml/|/jgzn/|/ldxx/|/zcfg/|/tzgg/|"
    r"/news/|/gov/|/about/|/contact/|/sitemap/|/search/|/login/|/register/)",
    re.IGNORECASE,
)


def _is_article_link(url, title):
    """判断链接是否像文章详情页，过滤导航/栏目/页脚噪声。"""
    if len(title) < 8:
        return False
    if _NAV_TITLE_PATTERNS.match(title.strip()):
        return False
    # 标题含日期或数字编号，通常是新闻标题
    if re.search(r"\d{4}年|\d{1,2}月|\d{1,2}日|第\d+期|〔\d{4}〕|\[\d{4}\]", title):
        return True
    # URL 匹配文章特征
    if _ARTICLE_URL_PATTERNS.search(url):
        return True
    # URL 匹配栏目特征则排除
    if _SECTION_URL_PATTERNS.search(url):
        return False
    # 其余：标题足够长（>=12字）且不是纯部门名，保留
    return len(title) >= 12


def parse_html_list(body, source_id, source_name, endpoint):
    parser = _LinkParser()
    parser.feed(_decode_document(body))
    items = []
    seen_urls = set()
    for href, title in parser.links:
        if not title or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = urljoin(endpoint, href)
        if url in seen_urls:
            continue
        try:
            validate_public_url(url)
        except ValueError:
            continue
        if not _is_article_link(url, title):
            continue
        seen_urls.add(url)
        items.append(
            ExternalItem(
                source_id=source_id,
                source_name=source_name,
                url=url,
                title=title,
                raw={"endpoint": endpoint},
            )
        )
    return items
