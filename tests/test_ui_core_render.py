"""ui_core.js 的**真执行**测试 —— 专门抓 N2：sourceBadgeHtml 的 title 未转义（潜在 XSS）。

为什么必须真执行：sourceBadgeHtml 把 candidate 的 provider 拼进 `title="${text}"`，
旧实现没走 escapeHtml。provider 来自后端元数据，绝大多数情况是可信的，但一旦混入
`&` / `"` / `<`（例如某个来源名带尖括号），未转义的 title 会破坏属性边界。
本测试用 node 动态 import 真实 ui_core.js（零 DOM 依赖），断言含特殊字符的 provider
在 title 与可见文本里都被转义。
"""

import os
import pathlib
import subprocess
import unittest
from urllib.request import pathname2url

STATIC = pathlib.Path(__file__).resolve().parents[1] / "src" / "yuanjian_app" / "static"
UI_CORE = STATIC / "js" / "ui_core.js"
NODE = os.environ.get("NODE", "node")


def file_url(path: pathlib.Path) -> str:
    return "file:" + pathname2url(str(path))


class UiCoreEscapeTests(unittest.TestCase):
    """真跑 ui_core.js 的纯函数。"""

    def run_ui_core(self, body: str):
        script = f"""
const assert = require('node:assert/strict');
(async () => {{
  const ui = await import({file_url(UI_CORE)!r});
  {{
{body}
  }}
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
        result = subprocess.run(
            [NODE, "-e", script],
            text=True, encoding="utf-8", capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_source_badge_title_is_escaped(self):
        # provider 故意带 & " < 三类特殊字符，整条文案须被转义
        self.run_ui_core(r"""
const c = { judgment_provider: 'a&b"c<d' };
const html = ui.sourceBadgeHtml(c);
assert.ok(html.includes('title="AI 研判 · a&amp;b&quot;c&lt;d"'), 'title 未转义: ' + html);
assert.ok(html.includes('>AI 研判 · a&amp;b&quot;c&lt;d<'), '可见文本未转义: ' + html);
assert.ok(!html.includes('title="AI 研判 · a&b"c<d"'), '仍含未转义尖括号/引号');
""")

    def test_source_badge_local_provider_no_regression(self):
        self.run_ui_core(r"""
const c = { judgment_provider: 'local' };
const html = ui.sourceBadgeHtml(c);
assert.ok(html.includes('模板推断 · 非真实研判'), '本机研判文案回退: ' + html);
""")


if __name__ == "__main__":
    unittest.main()
