"""诊断页（diag.js）的**真执行**测试 —— 抓暂停态硬编码「（密钥失效）」错文案（v1.5.2）。

后端把暂停来源分成两种并给出人话原因 ai_pause_reason：
  - 密钥无效/过期（auth）→ 原「密钥失效」对；
  - 连续多次调用失败触发的熔断（circuit_open）→ 「密钥失效」是错的，用户会去改没坏的密钥。

本测试把 diag.js 拿到 node 里真渲染：
  1) 熔断暂停：后端给 ai_pause_reason="连续多次调用失败，已暂停远程研判以免继续产生费用"，
     渲染出来必须是这段后端原文，且绝不能出现硬编码后缀「（密钥失效）」；
  2) 密钥失效暂停：后端给的原因也必须原样显示，不出现「（密钥失效）」；
  3) 远程已关闭：ai_enabled=false 且不暂停不回退 → 整页不得出现"已暂停"；
  4) 变异对照：把「只用后端原因」改回「硬编码（密钥失效）」→ 旧后缀复现 → 正常断言变红。

其余三态（正常/限流退避/本机回退）与禁用态隐藏状态条也一并断言，防止回归。
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
const api = async (url, opts) => (fixture[url] !== undefined ? fixture[url] : null);
const escapeHtml = (v) => String(v === null || v === undefined ? '' : v)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
const showPageError = () => {};
const showToast = () => {};
const alert = () => {};
const setTimeout = () => 0;
// diag.js 依赖的展示辅助：真库里来自 icons.js / ui_core.js，测试里给最小桩
// （必须与 diag.js 的导入名逐字一致：camelCase）。
const yjIcon = () => '';
const formatBytes = (v) => String(v === null || v === undefined ? '' : v);
const formatLocalTime = (v) => String(v === null || v === undefined ? '' : v);
const document = { createElement: () => ({ set innerHTML(_v) {}, appendChild() {} }), getElementById: () => null };
const location = { hash: '' };
`;

const exportsLine = `return { render };`;

function makeRoot() {
  let _html = '';
  const fakeEl = {
    addEventListener() {}, removeEventListener() {},
    getAttribute() { return null; }, setAttribute() {},
    querySelector() { return fakeEl; }, querySelectorAll() { return []; },
    classList: { add() {}, remove() {}, toggle() {} },
    style: {}, dataset: {}, textContent: '', value: '',
    appendChild() {}, remove() {}, focus() {},
    set innerHTML(v) {}, get innerHTML() { return ''; },
  };
  return {
    addEventListener() {}, removeEventListener() {},
    querySelector() { return fakeEl; }, querySelectorAll() { return []; },
    getAttribute() { return null; }, setAttribute() {},
    classList: { add() {}, remove() {}, toggle() {} },
    appendChild() {}, remove() {},
    set innerHTML(v) { _html = v; },
    get innerHTML() { return _html; },
  };
}

const out = { ok: false };
(async () => {
  try {
    const factory = new Function('fixture', stubs + src + exportsLine);
    const mod = factory(fixture);
    const root = makeRoot();
    await mod.render(root);
    out.html = root.innerHTML;
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
    fixture_path.write_text(json.dumps({"/api/diagnostics": diag}, ensure_ascii=False), encoding="utf-8")
    harness_path = tmp / "harness.js"
    harness_path.write_text(HARNESS_TEMPLATE, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(harness_path), str(module_path), str(fixture_path)],
        text=True, encoding="utf-8", capture_output=True, check=False, timeout=90,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise AssertionError(
            f"node 执行失败 rc={result.returncode}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
    return json.loads(result.stdout)


class DiagRemoteStatusBarTests(unittest.TestCase):
    """真跑 diag.js，验证四态状态条文案与用量显示（v1.5.2）。"""

    CIRCUIT_REASON = "连续多次调用失败，已暂停远程研判以免继续产生费用"
    KEY_REASON = "密钥无效或已过期，请到设置重新填写"

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

    def test_paused_circuit_open_shows_backend_reason(self):
        """熔断暂停：渲染必须是后端原文，不得出现硬编码「（密钥失效）」。"""
        outcome = _run_diag(DIAG_JS.read_text(encoding="utf-8"),
                            {"ai_enabled": True, "ai_jobs_today": 0, "ai_daily_budget": 200,
                             "ai_rate_limit_pending": 0, "ai_paused": True, "ai_pause_reason": self.CIRCUIT_REASON})
        self.assertTrue(outcome.get("ok"), f"渲染抛异常：{outcome.get('errorName')}: {outcome.get('error')}")
        self.assertIn(self.CIRCUIT_REASON, outcome["html"], "熔断暂停必须原样显示后端原因")
        self.assertNotIn("（密钥失效）", outcome["html"], "熔断场景绝不能再硬编码（密钥失效）")

    def test_paused_key_invalid_shows_backend_reason(self):
        """密钥失效暂停：渲染必须是后端原文，不得出现硬编码「（密钥失效）」。"""
        outcome = _run_diag(DIAG_JS.read_text(encoding="utf-8"),
                            {"ai_enabled": True, "ai_jobs_today": 3, "ai_daily_budget": 200,
                             "ai_rate_limit_pending": 0, "ai_paused": True, "ai_pause_reason": self.KEY_REASON})
        self.assertTrue(outcome.get("ok"), f"渲染抛异常：{outcome.get('error')}")
        self.assertIn(self.KEY_REASON, outcome["html"], "密钥失效暂停必须原样显示后端原因")
        self.assertNotIn("（密钥失效）", outcome["html"], "暂停态不得自己拼（密钥失效）后缀")

    def test_remote_off_not_shown_as_paused(self):
        """远程已关闭（用户自己关的）：整页不得出现"已暂停"，应是不启用表述。"""
        outcome = _run_diag(DIAG_JS.read_text(encoding="utf-8"),
                            {"ai_enabled": False, "ai_jobs_today": 0, "ai_daily_budget": 0, "ai_rate_limit_pending": 0})
        self.assertTrue(outcome.get("ok"), f"渲染抛异常：{outcome.get('error')}")
        self.assertNotIn("已暂停", outcome["html"], "远程已关闭不得显示成已暂停")
        self.assertIn("未启用", outcome["html"], "远程已关闭应显示未启用")

    def test_mutation_control_hardcoded_suffix_must_go_red(self):
        """变异对照：把「只用后端原因」改回「硬编码（密钥失效）」→ 旧后缀复现 → 正常断言变红，探针有牙。"""
        source = DIAG_JS.read_text(encoding="utf-8")
        mutated = source.replace(
            "    remoteCopy = aiPauseReason;",
            "    remoteCopy = `已暂停（密钥失效）：${aiPauseReason}`;",
        )
        self.assertNotEqual(mutated, source, "变异替换没生效，对照无效")

        outcome = _run_diag(mutated,
                            {"ai_enabled": True, "ai_jobs_today": 0, "ai_daily_budget": 200,
                             "ai_rate_limit_pending": 0, "ai_paused": True, "ai_pause_reason": self.CIRCUIT_REASON})
        self.assertTrue(outcome.get("ok"), f"变异版渲染抛异常：{outcome.get('error')}")
        # 变异版复现旧 bug：硬编码"（密钥失效）"出现 —— 正常断言（期望不含该后缀）必然变红。
        self.assertIn("（密钥失效）", outcome["html"], "变异对照失效：未复现硬编码后缀，探针没牙")


if __name__ == "__main__":
    unittest.main()
