"""移动摘要导出：生成自包含只读 HTML 到数据根 mobile/ 目录。

原则：
- 不引用任何本地资源、不内联脚本——单文件拷到手机即可离线阅读。
- 所有动态内容一律 html.escape，摘要页本身不可交互。
- 只含"今日"聚合视图：状态、计数、Top 风险、源覆盖。
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>远见 · 今日摘要 {generated}</title>
<style>
:root {{ color-scheme: dark; }}
body {{ margin: 0; background: #060A06; color: #C9D6CB;
  font: 14px/1.65 "Microsoft YaHei", system-ui, sans-serif; padding: 20px 16px 48px; }}
.wrap {{ max-width: 640px; margin: 0 auto; }}
h1 {{ font-size: 18px; color: #9EF0B0; letter-spacing: 2px; }}
.meta {{ color: #6E7F71; font-size: 12px; margin-bottom: 18px; }}
.sec {{ border: 1px solid #1E2B20; border-radius: 6px; padding: 12px 14px; margin: 12px 0; }}
.sec h2 {{ font-size: 14px; color: #FFB454; margin: 0 0 8px; letter-spacing: 1px; }}
.kv {{ display: flex; justify-content: space-between; padding: 3px 0; }}
.kv span:last-child {{ color: #9EF0B0; }}
.item {{ border-top: 1px dashed #1E2B20; padding: 8px 0; }}
.item:first-of-type {{ border-top: 0; }}
.lvl {{ font-size: 12px; color: #FFB454; }}
.dim {{ color: #6E7F71; font-size: 12px; }}
</style>
</head>
<body><div class="wrap">
<h1>远见 · 今日摘要</h1>
<p class="meta">生成于 {generated} · 只读快照 · 本机数据</p>
<div class="sec"><h2>当前状态</h2>
<div class="kv"><span>状态</span><span>{state}</span></div>
<div class="kv"><span>需行动</span><span>{action_count}</span></div>
<div class="kv"><span>观察中</span><span>{watch_count}</span></div>
<div class="kv"><span>信息源（启用/总）</span><span>{coverage}</span></div>
</div>
<p class="meta">{summary}</p>
{risk_html}
<div class="sec"><h2>说明</h2>
<p class="dim">本页由远见在本机生成，不含任何交互或外部请求；详细判读请回到桌面端查看。</p>
</div>
</div></body>
</html>"""


def _escape(value) -> str:
    return html.escape(str(value if value is not None else ""))


class MobileExportService:
    def __init__(self, export_dir, *, now=lambda: datetime.now(timezone.utc)):
        self.export_dir = Path(export_dir)
        self.now = now

    def render(self, dashboard: dict) -> str:
        current = self.now().astimezone()
        items = dashboard.get("items") or []
        risk_rows = []
        for item in items:
            risk_rows.append(
                '<div class="item">'
                f'<div>{_escape(item.get("title", "事件"))}</div>'
                f'<div class="lvl">{_escape(item.get("risk_label", ""))}'
                f' · 截止 {_escape(item.get("decision_by", ""))}</div>'
                f'<div class="dim">{_escape(item.get("advice", ""))}</div>'
                "</div>"
            )
        risk_html = (
            '<div class="sec"><h2>重点风险</h2>'
            + ("".join(risk_rows) if risk_rows else "<p class='dim'>今天没有高等级风险。</p>")
            + "</div>"
        )
        coverage = dashboard.get("coverage") or {}
        counts = dashboard.get("counts") or {}
        return PAGE_TEMPLATE.format(
            generated=current.strftime("%Y-%m-%d %H:%M"),
            state=_escape(dashboard.get("state", "unknown")),
            action_count=counts.get("action", 0),
            watch_count=counts.get("watch", 0),
            coverage=f"{coverage.get('enabled', 0)} / {coverage.get('healthy', 0)} 健康",
            summary=_escape(dashboard.get("summary", "")),
            risk_html=risk_html,
        )

    def export(self, dashboard: dict) -> dict:
        """渲染并落盘，返回 {path}（前端契约只读 path 键）。"""
        self.export_dir.mkdir(parents=True, exist_ok=True)
        stamp = self.now().astimezone().strftime("%Y%m%d-%H%M%S")
        target = self.export_dir / f"yuanjian-summary-{stamp}.html"
        target.write_text(self.render(dashboard), encoding="utf-8")
        return {"path": str(target)}
