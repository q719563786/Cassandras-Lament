"""Throttled local notification center with an optional Windows toast."""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from datetime import datetime, timedelta, timezone

# 事件簇去重的两道闸（2026-09-19 修）：
#   ① HARD_FLOOR_HOURS —— 硬下限。距上一条通知不足它的小时数时，**只有等级升高**
#      才允许再弹，其余一律抑制。此前只看 evidence_hash 变没变，而簇每吸收一条
#      同文转载哈希就变，6 小时节流被彻底绕过。
#   ② MATERIAL_WINDOW_HOURS —— 实质判据的比较窗口（同时也是"多久以后允许重新提醒"）。
#      窗口内比较证据等级 / 独立域名数 / 行动窗口档位，任一实质提升才放行。
HARD_FLOOR_HOURS = 6
MATERIAL_WINDOW_HOURS = 24

# 滚动 24 小时 Windows 弹窗总量上限。去重只能消掉"同一事件的重复"，消不掉
# "24 小时内 N 个不同事件各弹一次"——而 toast 文案是有意通用的，条数一多看起来
# 仍是一模一样。预算只决定"弹不弹系统通知"，不决定"进不进本地通知中心"：
# 超出预算的 L4 依然 status='unread'、依然在应用内可见，只是不弹窗，不丢信息。
DAILY_TOAST_BUDGET = 6

# 标题/正文走环境变量，不走命令行尾参：`powershell.exe -Command <脚本> <尾参>`
# **不会**把尾参绑进 `$args`（实测 $args 为空），尾参只会被当成命令文本执行，
# 于是弹出一张空的 toast 并返回非 0。改用环境变量后 argv 里再无尾参。
_TOAST_ENV_TITLE = "YUANJIAN_TOAST_TITLE"
_TOAST_ENV_BODY = "YUANJIAN_TOAST_BODY"


def _evidence_rank(level):
    """证据等级 → 序数；未知/空 → 0（低于任何真实等级）。"""
    return {"E4": 4, "E3": 3, "E2": 2, "E1": 1}.get(
        str(level or "").strip().upper(), 0
    )


def _window_rank(hours):
    """行动窗口 → 紧迫度序数（值越大＝窗口越紧）。None → 0。"""
    if hours is None:
        return 0
    try:
        value = float(hours)
    except (TypeError, ValueError):
        return 0
    if value <= 24:
        return 4
    if value <= 72:
        return 3
    if value <= 168:
        return 2
    return 1


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _toast_invocation(title, body):
    """构造 toast 的 (argv, env)。

    标题与正文**只经环境变量**传递，argv 末尾不得再出现它们——`-Command` 的
    尾参不会进入 `$args`，只会被当命令文本执行（见模块顶部注释）。抽成纯函数
    是为了让"传参方式"可被单测直接断言，不必真的弹窗。
    """
    script = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null
$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$nodes = $template.GetElementsByTagName('text')
$nodes.Item(0).AppendChild($template.CreateTextNode($env:YUANJIAN_TOAST_TITLE)) > $null
$nodes.Item(1).AppendChild($template.CreateTextNode($env:YUANJIAN_TOAST_BODY)) > $null
$toast = [Windows.UI.Notifications.ToastNotification]::new($template)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('远见').Show($toast)
"""
    argv = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-WindowStyle",
        "Hidden",
        "-Command",
        script,
    ]
    env = dict(os.environ)
    env[_TOAST_ENV_TITLE] = str(title)
    env[_TOAST_ENV_BODY] = str(body)
    return argv, env


def _windows_notifier(title, body):
    argv, env = _toast_invocation(title, body)
    completed = subprocess.run(
        argv,
        env=env,
        capture_output=True,
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("windows_toast_failed")


class NotificationService:
    def __init__(self, database, notifier=None, now=None):
        self.database = database
        self.notifier = notifier or _windows_notifier
        self.now = now or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _metadata(row):
        try:
            value = json.loads(row["reason"])
        except (TypeError, json.JSONDecodeError):
            return {"summary": row["reason"], "action_window_hours": None}
        return value if isinstance(value, dict) else {"summary": str(value)}

    def _recent(self, cluster_id, now, hours):
        cutoff = _iso(now - timedelta(hours=hours))
        with self.database.connect() as connection:
            return connection.execute(
                """
                SELECT * FROM notification_log
                WHERE cluster_id=? AND created_at>=?
                ORDER BY created_at DESC,notification_id DESC LIMIT 1
                """,
                (cluster_id, cutoff),
            ).fetchone()

    def _windows_budget_used(self, now) -> int:
        """滚动 24 小时内已成功弹出的系统通知条数。"""
        cutoff = _iso(now - timedelta(hours=24))
        with self.database.connect() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM notification_log "
                "WHERE delivery='windows' AND created_at>=?",
                (cutoff,),
            ).fetchone()[0]

    def consider(self, impact: dict, reason: str) -> dict:
        level = str(impact.get("alert_level", "L1"))
        if level not in {"L1", "L2", "L3", "L4"}:
            raise ValueError("通知等级无效")
        if level == "L1":
            return {"status": "ignored", "delivery": "none"}
        now = self.now().astimezone(timezone.utc)
        cluster_id = str(impact.get("cluster_id", ""))
        if not cluster_id:
            raise ValueError("通知缺少事件簇")
        evidence_hash = str(impact.get("evidence_hash", ""))
        evidence_level = str(impact.get("evidence_level") or "")
        independent_domains = impact.get("independent_domains")
        try:
            independent_domains = (
                None if independent_domains is None else int(independent_domains)
            )
        except (TypeError, ValueError):
            independent_domains = None
        window = impact.get("action_window_hours")
        window = None if window is None else max(0, int(window))
        recent = self._recent(cluster_id, now, MATERIAL_WINDOW_HOURS)
        if recent is not None:
            previous = self._metadata(recent)
            previous_window = previous.get("action_window_hours")
            previous_ind = previous.get("independent_domains")
            try:
                previous_ind = None if previous_ind is None else int(previous_ind)
            except (TypeError, ValueError):
                previous_ind = None
            hours_since = (
                now - _parse(recent["created_at"])
            ).total_seconds() / 3600
            level_up = int(level[1:]) > int(recent["alert_level"][1:])
            evidence_up = _evidence_rank(evidence_level) > _evidence_rank(
                previous.get("evidence_level", "")
            )
            sources_grew = (
                previous_ind is not None
                and independent_domains is not None
                and independent_domains > previous_ind
            )
            window_tightened = _window_rank(window) > _window_rank(previous_window)
            # 旧记录没有 evidence_level / independent_domains，无法做实质比较；
            # 只对它们保留"哈希变化"这一向后兼容判据，新记录永远走实质判据。
            legacy = (
                "evidence_level" not in previous
                and "independent_domains" not in previous
            )
            legacy_changed = legacy and evidence_hash != recent["evidence_hash"]
            material = (
                evidence_up or sources_grew or window_tightened or legacy_changed
            )
            # 硬下限：不足 HARD_FLOOR_HOURS 只有等级升高能突破；否则需实质提升。
            if hours_since < HARD_FLOOR_HOURS:
                allow = level_up
            else:
                allow = level_up or material
            if not allow:
                return {"status": "suppressed", "delivery": "none"}

        error_message = ""
        if level == "L2":
            delivery = "daily_digest"
            status = "digest"
        elif level == "L3":
            delivery = "local_only"
            status = "unread"
        else:
            status = "unread"
            if self._windows_budget_used(now) >= DAILY_TOAST_BUDGET:
                # 配额用尽不是发送失败：error_message 保持空，只降级为本地通知。
                delivery = "local_only"
            else:
                try:
                    self.notifier(
                        "远见：高优先级事件",
                        "发现一项需要立即查看的外部事件，请打开远见查看证据和行动窗口。",
                    )
                    delivery = "windows"
                except Exception as error:
                    delivery = "local_only"
                    error_message = type(error).__name__

        notification_id = "D-" + uuid.uuid4().hex
        metadata = json.dumps(
            {
                "summary": " ".join(str(reason or "").split()),
                "action_window_hours": window,
                "evidence_level": evidence_level,
                "independent_domains": independent_domains,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO notification_log(
                    notification_id,cluster_id,impact_id,created_at,alert_level,
                    reason,evidence_hash,status,delivery,error_message
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    notification_id,
                    cluster_id,
                    impact.get("impact_id"),
                    _iso(now),
                    level,
                    metadata,
                    evidence_hash,
                    status,
                    delivery,
                    error_message,
                ),
            )
        return {
            "notification_id": notification_id,
            "status": "created",
            "delivery": delivery,
        }

    def list_page(self, limit: int = 20, offset: int = 0, status: str = "") -> dict:
        limit = int(limit)
        offset = int(offset)
        status = str(status or "").strip().casefold()
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("分页参数无效")
        if status not in {"", "unread", "read", "digest"}:
            raise ValueError("通知状态无效")
        where = " WHERE status=?" if status else ""
        values = [status] if status else []
        with self.database.connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM notification_log{where}", values
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                SELECT * FROM notification_log
                {where}
                ORDER BY created_at DESC,notification_id DESC LIMIT ? OFFSET ?
                """,
                (*values, limit, offset),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            metadata = self._metadata(row)
            item["reason"] = metadata.get("summary", "")
            item["action_window_hours"] = metadata.get("action_window_hours")
            output.append(item)
        return {"items": output, "total": total, "limit": limit, "offset": offset}

    def list_notifications(self, limit: int = 100) -> list[dict]:
        safe_limit = max(1, min(int(limit), 100))
        return self.list_page(limit=safe_limit)["items"]

    def mark_read(self, notification_id: str) -> dict:
        now = _iso(self.now())
        with self.database.connect() as connection:
            result = connection.execute(
                """
                UPDATE notification_log SET status='read',read_at=?
                WHERE notification_id=?
                """,
                (now, notification_id),
            )
            if result.rowcount != 1:
                raise KeyError(notification_id)
        return {"notification_id": notification_id, "status": "read"}

    def mark_all_read(self) -> dict:
        now = _iso(self.now())
        with self.database.connect() as connection:
            result = connection.execute(
                """
                UPDATE notification_log SET status='read',read_at=?
                WHERE status='unread'
                """,
                (now,),
            )
        return {"status": "read", "updated": result.rowcount}
