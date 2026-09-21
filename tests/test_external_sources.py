import gzip
import json
import socket
import ssl
import unittest
import urllib.error

from yuanjian_app.external_sources import (
    MAX_DECOMPRESSED_BYTES,
    FetchError,
    fetch_bytes,
    fetch_json,
    parse_html_list,
    parse_feed,
    parse_gdelt,
    parse_json_api,
    validate_public_url,
)


RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><item>
<title>Policy update</title><link>https://example.com/policy/1</link>
<description>New reimbursement rule</description>
<pubDate>Thu, 07 Aug 2026 08:00:00 GMT</pubDate>
</item></channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><entry>
<title>Project notice</title><link href="https://example.com/project/2"/>
<summary>Public tender information</summary><updated>2026-08-07T08:30:00Z</updated>
</entry></feed>"""


class Response:
    def __init__(self, body, headers=None):
        self.body = body
        self.headers = headers if headers is not None else {}

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def request_headers(request):
    """urllib 会把头名规范化（`Accept-Encoding` → `Accept-encoding`），这里统一小写读。"""
    return {key.lower(): value for key, value in request.headers.items()}


def public_resolver(host, port, type):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


class ExternalSourceTests(unittest.TestCase):
    def test_rss_and_atom_are_normalized_to_the_same_item_contract(self):
        rss = parse_feed(RSS, "S-rss", "Policy feed", "https://example.com/rss")
        atom = parse_feed(ATOM, "S-atom", "Tender feed", "https://example.com/atom")

        self.assertEqual(rss[0].title, "Policy update")
        self.assertEqual(rss[0].url, "https://example.com/policy/1")
        self.assertEqual(rss[0].summary, "New reimbursement rule")
        self.assertEqual(rss[0].published_at, "2026-08-07T08:00:00Z")
        self.assertEqual(atom[0].title, "Project notice")
        self.assertEqual(atom[0].url, "https://example.com/project/2")
        self.assertEqual(atom[0].published_at, "2026-08-07T08:30:00Z")

    def test_gdelt_articles_are_normalized_without_losing_provenance(self):
        body = json.dumps(
            {
                "articles": [
                    {
                        "url": "https://news.example.cn/a",
                        "title": "Gold price movement",
                        "seendate": "20260807T090000Z",
                        "domain": "news.example.cn",
                        "language": "Chinese",
                        "sourcecountry": "China",
                    }
                ]
            }
        ).encode()

        item = parse_gdelt(body, "S-gdelt", "GDELT")[0]

        self.assertEqual(item.url, "https://news.example.cn/a")
        self.assertEqual(item.language, "Chinese")
        self.assertEqual(item.source_id, "S-gdelt")
        self.assertIn("news.example.cn", item.summary)

    def test_url_safety_rejects_local_private_and_non_http_targets(self):
        for unsafe in (
            "file:///C:/secret.txt",
            "http://localhost/admin",
            "http://127.0.0.1:8080/",
            "http://192.168.1.5/",
            "http://169.254.1.1/",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                validate_public_url(unsafe)

        self.assertEqual(
            validate_public_url("https://example.com/feed.xml"),
            "https://example.com/feed.xml",
        )

    def test_fetch_enforces_response_limit(self):
        def opener(request, timeout):
            return Response(b"x" * 9)

        with self.assertRaisesRegex(FetchError, "响应超过") as raised:
            fetch_bytes(
                "https://example.com/feed",
                opener=opener,
                max_bytes=8,
                resolver=public_resolver,
            )

        self.assertEqual(raised.exception.error_type, "too_large")

    def test_fetch_classifies_timeout(self):
        def opener(request, timeout):
            raise socket.timeout("slow")

        with self.assertRaises(FetchError) as raised:
            fetch_bytes(
                "https://example.com/feed",
                opener=opener,
                timeout=1,
                resolver=public_resolver,
            )

        self.assertEqual(raised.exception.error_type, "timeout")

    def test_fetch_rejects_hostname_that_resolves_to_private_network(self):
        def private_resolver(host, port, type):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 0))]

        with self.assertRaises(FetchError) as raised:
            fetch_bytes(
                "https://public-looking.example/feed",
                opener=lambda request, timeout: Response(b"safe"),
                resolver=private_resolver,
            )

        self.assertEqual(raised.exception.error_type, "unsafe_url")

    def test_public_html_list_extracts_only_meaningful_links(self):
        body = """<html><body>
        <a href='/notice/1'>河源市工程项目招标公告</a>
        <a href='#top'>顶部</a><a href='javascript:void(0)'>无效链接</a>
        </body></html>""".encode("utf-8")

        items = parse_html_list(
            body, "S-html", "河源公开信息", "https://example.com/notices/"
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].url, "https://example.com/notice/1")
        self.assertEqual(items[0].title, "河源市工程项目招标公告")


JSON_API = json.dumps(
    {
        "data": {
            "pageData": [
                {"title": "招标公告", "url": "https://example.com/t/1",
                 "publishDate": "2026-08-07"},
                {"title": "中标结果", "url": "https://example.com/t/2",
                 "publishDate": "2026-08-08"},
            ]
        }
    }
).encode("utf-8")

JSON_API_CONFIG = {
    "items_path": "data.pageData",
    "fields": {"title": "title", "url": "url", "published_at": "publishDate"},
}


def certificate_error():
    """证书校验失败：urllib 会把它包进 URLError.reason。"""
    return urllib.error.URLError(
        ssl.SSLCertVerificationError("certificate verify failed")
    )


class GzipAndSslFallbackTests(unittest.TestCase):
    """gzip 响应体解压 + SSL 降级回退（两条抓取路径行为必须一致）。

    全部离线：夹具用 `gzip.compress` 现造，`opener` 打桩，DNS 用公网 resolver 桩。
    """

    def fetch_and_parse(self, body, headers=None, *, path="bytes", opener=None):
        """走真实的 fetch_bytes / fetch_json 再用真实解析器，返回条目列表。"""
        if opener is None:
            def opener(request, timeout):
                return Response(body, headers)

        if path == "bytes":
            raw = fetch_bytes(
                "https://example.com/feed", opener=opener, resolver=public_resolver
            )
            return parse_feed(raw, "S-rss", "feed", "https://example.com/rss")
        raw = fetch_json(
            "https://example.com/api", {}, opener=opener, resolver=public_resolver
        )
        return parse_json_api(raw, "S-json", "api", JSON_API_CONFIG)

    def test_gzip_body_is_decompressed_so_item_count_matches_plain_version(self):
        for path in ("bytes", "json"):
            with self.subTest(path=path):
                plain = RSS if path == "bytes" else JSON_API
                compressed = gzip.compress(plain)
                self.assertNotEqual(compressed, plain)
                expected = len(self.fetch_and_parse(plain, None, path=path))
                # 夹具本身必须能解析出条目，否则断言等于没断言
                self.assertGreater(expected, 0)
                self.assertEqual(
                    len(
                        self.fetch_and_parse(
                            compressed, {"Content-Encoding": "gzip"}, path=path
                        )
                    ),
                    expected,
                )

    def test_gzip_magic_bytes_without_a_header_are_decompressed_too(self):
        # 有些站点无视 Accept-Encoding: identity，照样压 —— 且不总是老实声明
        compressed = gzip.compress(RSS)

        items = self.fetch_and_parse(compressed, None)

        self.assertEqual(len(items), 1)

    def test_declared_gzip_with_a_corrupt_payload_raises_an_explicit_error(self):
        for body in (b"\x1f\x8bnot really gzip", b"plain text, no gzip here"):
            with self.subTest(body=body):
                with self.assertRaises(FetchError) as raised:
                    self.fetch_and_parse(body, {"Content-Encoding": "gzip"})
                # 明确报错，不被静默吞掉、也不当成"能解析的空正文"
                self.assertEqual(raised.exception.error_type, "bad_encoding")

    def test_decompressed_size_is_capped_so_a_gzip_bomb_is_rejected(self):
        bomb = gzip.compress(b"\0" * (MAX_DECOMPRESSED_BYTES + 1))
        # 线上体积很小（远低于 max_bytes），只有解压后才爆炸
        self.assertLess(len(bomb), 1024 * 1024)

        with self.assertRaises(FetchError) as raised:
            self.fetch_and_parse(bomb, {"Content-Encoding": "gzip"})

        self.assertEqual(raised.exception.error_type, "too_large")

    def test_both_paths_ask_the_server_not_to_compress(self):
        seen = {}

        def opener(request, timeout):
            seen.update(request_headers(request))
            return Response(RSS)

        fetch_bytes("https://example.com/feed", opener=opener, resolver=public_resolver)
        self.assertEqual(seen.get("accept-encoding"), "identity")

        def json_opener(request, timeout):
            seen.update(request_headers(request))
            return Response(JSON_API)

        fetch_json(
            "https://example.com/api", {}, opener=json_opener, resolver=public_resolver
        )
        self.assertEqual(seen.get("accept-encoding"), "identity")

    def test_certificate_failure_retries_unverified_on_both_paths_and_leaves_a_trace(self):
        class CertBrokenOpener:
            def __init__(self):
                self.contexts = []

            def __call__(self, request, timeout, context=None):
                self.contexts.append(context)
                if context is None:  # 首次：校验证书 → 失败
                    raise certificate_error()
                return Response(RSS)

        opener = CertBrokenOpener()

        with self.assertLogs("yuanjian_app.external_sources", level="WARNING") as logs:
            items = self.fetch_and_parse(RSS, None, opener=opener)

        self.assertEqual(len(items), 1)  # 降级后成功
        # 只降级一次：先校验，再带 context 不校验重试
        self.assertEqual([context is None for context in opener.contexts], [True, False])
        # 留痕：只记主机名，不带 query
        self.assertTrue(any("TLS证书校验失败" in line for line in logs.output))
        self.assertTrue(any("host=example.com" in line for line in logs.output))

    def test_fetch_json_uses_the_same_fallback_as_fetch_bytes(self):
        class CertBrokenOpener:
            def __call__(self, request, timeout, context=None):
                if context is None:
                    raise certificate_error()
                return Response(JSON_API)

        with self.assertLogs("yuanjian_app.external_sources", level="WARNING"):
            items = self.fetch_and_parse(JSON_API, None, path="json", opener=CertBrokenOpener())

        self.assertEqual(len(items), 2)

    def test_when_the_retry_also_fails_the_source_is_reported_unreachable(self):
        class AlwaysBrokenOpener:
            def __call__(self, request, timeout, context=None):
                raise certificate_error()

        for path in ("bytes", "json"):
            with self.subTest(path=path):
                with self.assertLogs(
                    "yuanjian_app.external_sources", level="WARNING"
                ):
                    with self.assertRaisesRegex(FetchError, "不可达") as raised:
                        self.fetch_and_parse(
                            RSS, None, path=path, opener=AlwaysBrokenOpener()
                        )
                self.assertEqual(raised.exception.error_type, "unreachable")


if __name__ == "__main__":
    unittest.main()
