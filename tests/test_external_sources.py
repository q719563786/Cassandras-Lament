import json
import socket
import unittest

from yuanjian_app.external_sources import (
    FetchError,
    fetch_bytes,
    parse_html_list,
    parse_feed,
    parse_gdelt,
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
    def __init__(self, body):
        self.body = body

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


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


if __name__ == "__main__":
    unittest.main()
