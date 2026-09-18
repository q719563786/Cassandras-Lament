"""检查有没有新版本。

只在用户主动点击「检查更新」时发一次请求，不做任何后台轮询、不自动下载。
请求本身只取公开发布信息，不携带本机的任何数据（不发版本号、不发设备信息、
不发使用情况）。

除了采集公开信息源（那本来就是本程序的功能），这是唯一由用户主动触发的外发
请求，因此单独写在 PRIVACY.md 里说明。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

DEFAULT_ENDPOINT = (
    "https://api.github.com/repos/q719563786/Cassandras-Lament/releases/latest"
)
RELEASES_PAGE = "https://github.com/q719563786/Cassandras-Lament/releases"


def parse_version(text):
    """把 "v1.2.3" / "1.2.3" / "1.2" 解析成可比较的整数元组，解析不了返回 None。"""
    if not text:
        return None
    cleaned = str(text).strip().lstrip("vV")
    parts = cleaned.split(".")
    numbers = []
    for part in parts:
        digits = ""
        for character in part:
            if character.isdigit():
                digits += character
            else:
                break
        if not digits:
            return None
        numbers.append(int(digits))
    return tuple(numbers) if numbers else None


def is_newer(latest, current):
    """latest 是否比 current 新。任一侧解析不了就返回 False（宁可不说有新版本）。"""
    latest_version = parse_version(latest)
    current_version = parse_version(current)
    if latest_version is None or current_version is None:
        return False
    return latest_version > current_version


def _default_opener(request, timeout):
    return urllib.request.urlopen(request, timeout=timeout)


class UpdateCheckService:
    """查询公开的 Release 信息并与本地版本比较。"""

    def __init__(self, endpoint=DEFAULT_ENDPOINT, opener=None, timeout=8):
        self.endpoint = endpoint
        self.opener = opener or _default_opener
        self.timeout = timeout

    def check(self, current_version):
        request = urllib.request.Request(
            self.endpoint,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "YuanJian-UpdateCheck",
            },
        )
        try:
            with self.opener(request, self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            # 仓库没有发布 Release 时 GitHub 会返回 404，这不是故障。
            if error.code == 404:
                return self._result(current_version, status="no_release")
            return self._result(current_version, status="unreachable")
        except Exception:
            # 断网、超时、证书问题、返回体不是 JSON —— 一律按"查不到"处理，
            # 不把网络异常暴露成错误弹窗。
            return self._result(current_version, status="unreachable")

        latest = payload.get("tag_name") or payload.get("name") or ""
        if not latest:
            return self._result(current_version, status="no_release")
        return self._result(
            current_version,
            status="ok",
            latest=str(latest),
            has_update=is_newer(latest, current_version),
            page=payload.get("html_url") or RELEASES_PAGE,
        )

    def _result(self, current_version, *, status, latest="", has_update=False, page=RELEASES_PAGE):
        return {
            "status": status,
            "current": current_version,
            "latest": latest,
            "has_update": has_update,
            "releases_url": page,
        }
