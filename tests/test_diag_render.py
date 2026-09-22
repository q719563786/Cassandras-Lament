"""诊断页（diag.js）的**真执行**测试 —— 专门抓 N3 远程研判真实状态条。

为什么必须真执行：状态条是纯字符串拼接 + 四态分支，文本断言容易，但"去掉某一态的判断
测试会不会变红"才是关键 —— 不在 node 里真跑一遍，无法确认这套四态逻辑真的接到了渲染出口，
也无法确认变异对照有牙。

本测试：
  1) 四种状态各自的 diag 夹具下，断言状态条出现对应的**大白话**文案，且显示「今天用了 X / Y 次」；
  2) 未启用时不显示状态条；
  3) 变异对照：去掉「已暂停（密钥失效）」那一分支 → 暂停夹具下状态条消失 → 测试必须变红。

注意：ai_paused / ai_pause_reason / ai_fallback_local / ai_fallback_reason 由后端补充暴露；
本批次后端尚未添加时恒为 undefined，那两态不触发（属预期占位）。本测试直接喂这些字段，
证明前端逻辑已就绪，后端一补字段即生效。
"""

import json
import os
import pathlib
import re
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "yuanjian_app" / "static"
DIAG_JS = STATIC / "js" / "views" / "diag.js"
NODE = os.environ.get("NODE", "node")

HARNESS_TEMPLATE = r"""
const fs = require('fs');
const modulePath = process.argv[2];
const fixturePath = process.argv[3];
let src = fs.readFileSync(modulePath, 'utf8');
src = src.replace(/^import[^;]*;\s*$/mg, '');
src = src.replace(/^export\s+/gm, '');

const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));
const stubs = `
const __diag = __DIAG_JSON__;
const api = async () => __diag;
const escapeHtml = (v) => String(v === null || v === undefined ? '' : v)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
const showPageError = () => {};
const yjIcon = () => '';
const format_bytes = (n) => String(n);
const format_local_time = (s) => String(s || '');
`;
const exportsLine = `return { render };`;
const root = {
  _html: '',
  set innerHTML(v) { this._html = v; },
  get innerHTML() { return this._html; },
};
const out = { ok: false };
(async () => {
  try {
    const factory = new Function(stubs + src + exportsLine);
    const mod = factory();
    await mod.render(root);
    out.html = root._html;
    out.ok = true;
  } catch (error) {
    out.error = String(error && error.message ? error.message : error);
    out.errorName = String(error && error.name ? error.name : '');
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


def _run_diag(source_text: str, diag: dict) -> dict:
    tmp = pathlib.Path(tempfile.mkdtemp())
    module_path = tmp / "diag_under_test.js"
    module_path.write_text(source_text, encoding="utf-8")
    fixture_path = tmp / "fixture.json"
    fixture_path.write_text(json.dumps(diag, ensure_ascii=False), encoding="utf-8")
    script = HARNESS_TEMPLATE.replace("__DIAG_JSON__", json.dumps(diag, ensure_ascii=False))
    harness_path = tmp / "harness.js"
    harness_path.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(harness_path), str(module_path), str(fixture_path)],
        text=True, encoding="utf-8", capture_output=True, check=False, timeout=90,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise AssertionError(
            f"node 执行失败 rc={result.returncode}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
    return json.loads(result.stdout)


@unittest.skipUnless(os.name == "nt" or True, "需要 node")
class DiagRemoteStatusBarTests(unittest.TestCase):
    """真跑 diag.js，验证四态状态条文案与用量显示。"""

    def test_normal_state_shows_online_and_usage(self):
        outcome = _run_diag(DIAG_JS.read_text(encoding="utf-8"),
                            {"ai_enabled": True, "ai_jobs_today": 12, "ai_daily_budget": 200, "ai_rate_limit_pending": 0})
        self.assertTrue(outcome.get("ok"), f"渲染抛异常：{outcome.get('error')}")
        self.assertIn("远程研判：正常 · 远程研判在线", outcome["html"])
        self.assertIn("今天用了 12 / 200 次", outcome["html"])

    def test_ratelimit_state_shows_backoff(self):
        outcome = _run_diag(DIAG_JS.read_text(encoding="utf-8"),
                            {"ai_enabled": True, "ai_jobs_today": 5, "ai_daily_budget": 200, "ai_rate_limit_pending": 3})
        self.assertIn("正常 · 但有限流退避：3 个远程作业在等", outcome["html"])

    def test_fallback_state_says_switched_to_local(self):
        outcome = _run_diag(DIAG_JS.read_text(encoding="utf-8"),
                            {"ai_enabled": True, "ai_jobs_today": 9, "ai_daily_budget": 200,
                             "ai_rate_limit_pending": 0, "ai_fallback_local": True, "ai_fallback_reason": "密钥无效"})
        self.assertIn("暂时连不上，已改用本机研判（上次失败：密钥无效）", outcome["html"])

    def test_paused_state_says_key_invalid(self):
        outcome = _run_diag(DIAG_JS.read_text(encoding="utf-8"),
                            {"ai_enabled": True, "ai_jobs_today": 0, "ai_daily_budget": 200,
                             "ai_rate_limit_pending": 0, "ai_paused": True, "ai_pause_reason": "密钥已过期"})
        self.assertIn("已暂停（密钥失效）：密钥已过期", outcome["html"])

    def test_disabled_state_hides_status_bar(self):
        outcome = _run_diag(DIAG_JS.read_text(encoding="utf-8"),
                            {"ai_enabled": False, "ai_jobs_today": 0, "ai_daily_budget": 0, "ai_rate_limit_pending": 0})
        self.assertNotIn("远程研判：", outcome["html"])

    def test_mutation_control_removing_paused_branch_must_go_red(self):
        """让「已暂停（密钥失效）」分支变成死分支（if(false)） → 暂停夹具下状态条消失 → 测试必须变红。"""
        source = DIAG_JS.read_text(encoding="utf-8")
        mutated = re.sub(r"if \(aiPaused\)", "if (false)", source)
        self.assertNotEqual(mutated, source, "变异替换没生效，对照无效")

        outcome = _run_diag(mutated,
                            {"ai_enabled": True, "ai_jobs_today": 0, "ai_daily_budget": 200,
                             "ai_rate_limit_pending": 0, "ai_paused": True, "ai_pause_reason": "密钥已过期"})
        self.assertTrue(outcome.get("ok"), f"变异版渲染抛异常：{outcome.get('error')}")
        self.assertNotIn(
            "已暂停（密钥失效）",
            outcome["html"],
            "去掉暂停分支后，暂停状态应不再显示 —— 否则本探针没牙",
        )


if __name__ == "__main__":
    unittest.main()
