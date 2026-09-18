import json
import re
import unittest
import urllib.error
from pathlib import Path

from yuanjian_app import __version__
from yuanjian_app.update_check import (
    RELEASES_PAGE,
    UpdateCheckService,
    is_newer,
    parse_version,
)


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def opener_returning(payload):
    def opener(request, timeout):
        return FakeResponse(payload)

    return opener


def opener_raising(error):
    def opener(request, timeout):
        raise error

    return opener


class VersionParsingTests(unittest.TestCase):
    def test_version_strings_are_parsed_with_or_without_prefix(self):
        self.assertEqual(parse_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(parse_version("1.2.3"), (1, 2, 3))
        self.assertEqual(parse_version("V2.0"), (2, 0))
        self.assertEqual(parse_version("1.0.0-beta"), (1, 0, 0))

    def test_unparsable_versions_return_none(self):
        for value in (None, "", "   ", "v", "nightly", "abc.def"):
            self.assertIsNone(parse_version(value), f"{value!r} 不应被解析成版本号")

    def test_newer_comparison_orders_numerically_not_lexically(self):
        self.assertTrue(is_newer("v1.10.0", "1.9.0"))
        self.assertTrue(is_newer("1.0.1", "1.0.0"))
        self.assertFalse(is_newer("1.0.0", "1.0.0"))
        self.assertFalse(is_newer("0.9.0", "1.0.0"))

    def test_unparsable_side_is_treated_as_no_update(self):
        # 宁可不说有新版本，也不要在解析失败时误导用户去下载。
        self.assertFalse(is_newer("nightly", "1.0.0"))
        self.assertFalse(is_newer("2.0.0", "unknown"))


class UpdateCheckServiceTests(unittest.TestCase):
    def service(self, opener):
        return UpdateCheckService(
            endpoint="https://api.github.com/repos/example/repo/releases/latest",
            opener=opener,
        )

    def test_newer_release_is_reported_with_its_page(self):
        service = self.service(
            opener_returning(
                {"tag_name": "v2.0.0", "html_url": "https://example.com/releases/v2.0.0"}
            )
        )

        result = service.check("1.0.0")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["current"], "1.0.0")
        self.assertEqual(result["latest"], "v2.0.0")
        self.assertTrue(result["has_update"])
        self.assertEqual(result["releases_url"], "https://example.com/releases/v2.0.0")

    def test_same_release_reports_no_update(self):
        service = self.service(opener_returning({"tag_name": "v1.0.0"}))

        result = service.check("1.0.0")

        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["has_update"])
        # 没有 html_url 时退回固定的发布页，不能给出空链接。
        self.assertEqual(result["releases_url"], RELEASES_PAGE)

    def test_missing_release_is_not_an_error(self):
        service = self.service(opener_raising(urllib.error.HTTPError(
            "url", 404, "Not Found", {}, None
        )))

        result = service.check("1.0.0")

        self.assertEqual(result["status"], "no_release")
        self.assertFalse(result["has_update"])

    def test_network_failure_degrades_quietly(self):
        for error in (
            urllib.error.URLError("offline"),
            TimeoutError("timeout"),
            ValueError("不是 JSON"),
        ):
            with self.subTest(error=type(error).__name__):
                service = self.service(opener_raising(error))

                result = service.check("1.0.0")

                # 断网、超时、返回体异常都必须降级成"查不到"，
                # 不能变成错误弹窗，也绝不能谎报有新版本。
                self.assertEqual(result["status"], "unreachable")
                self.assertFalse(result["has_update"])

    def test_release_without_a_tag_is_treated_as_no_release(self):
        service = self.service(opener_returning({"name": ""}))

        result = service.check("1.0.0")

        self.assertEqual(result["status"], "no_release")

    def test_request_carries_no_local_data(self):
        """检查更新只能取公开发布信息，请求里不得带本机信息。"""
        captured = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = {k.lower(): v for k, v in request.header_items()}
            return FakeResponse({"tag_name": "v1.0.0"})

        self.service(opener).check(__version__)

        self.assertEqual(
            captured["url"],
            "https://api.github.com/repos/example/repo/releases/latest",
        )
        joined = " ".join(f"{k}={v}" for k, v in captured["headers"].items())
        # 只允许固定的 Accept 与 User-Agent，不得出现版本号、路径或主机名。
        for leaked in (__version__, "C:", "Users", "DESKTOP"):
            self.assertNotIn(leaked, joined, f"请求头里出现了本机信息：{leaked}")
        self.assertEqual(sorted(captured["headers"]), ["accept", "user-agent"])

    def test_default_endpoint_and_page_point_at_this_repository(self):
        service = UpdateCheckService()

        self.assertIn("q719563786/Cassandras-Lament", service.endpoint)
        self.assertIn("q719563786/Cassandras-Lament", RELEASES_PAGE)
        self.assertTrue(RELEASES_PAGE.startswith("https://"))


class UpdateCheckSourceTests(unittest.TestCase):
    def test_module_only_does_network_io(self):
        """这个模块只允许联网取公开信息，不得触碰本机文件或环境。"""
        source = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "yuanjian_app"
            / "update_check.py"
        ).read_text(encoding="utf-8")

        # 读取本机文件需要 open()；urlopen() 是网络调用，不属于此列。
        self.assertIsNone(
            re.search(r"(?<!url)\bopen\(", source),
            "更新检查不应读取本机文件",
        )
        for forbidden in ("pathlib", "subprocess", "os.environ", "import os"):
            self.assertNotIn(forbidden, source, f"更新检查不应出现 {forbidden}")

    def test_module_uses_only_the_fixed_public_endpoint(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "yuanjian_app"
            / "update_check.py"
        ).read_text(encoding="utf-8")

        urls = re.findall(r"https://[^\s\"']+", source)
        self.assertTrue(urls, "没有找到任何地址，扫描逻辑可能失效")
        for url in urls:
            self.assertTrue(
                url.startswith("https://api.github.com/")
                or url.startswith("https://github.com/"),
                f"出现了预期之外的地址：{url}",
            )


if __name__ == "__main__":
    unittest.main()
