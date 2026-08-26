import ipaddress
import json
import socket
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


def parse_html_list(body, source_id, source_name, endpoint):
    parser = _LinkParser()
    parser.feed(_decode_document(body))
    items = []
    for href, title in parser.links:
        if len(title) < 6 or href.startswith(("#", "javascript:", "mailto:")):
            continue
        url = urljoin(endpoint, href)
        try:
            validate_public_url(url)
        except ValueError:
            continue
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
