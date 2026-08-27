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
