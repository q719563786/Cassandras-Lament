"""设置页（settings.js）的**真执行**测试 —— 抓硬编码每日上限 2000 与后端默认 200 漂移。

后端把"非本地 provider 的每日上限"默认值从 2000 改成 200（2000 是照免费 Agnes
端点定的）。settings.js 此前三处写死 2000。本测试把 settings.js 拿到 node 里真渲染：

  1) 后端未返回值时（ai 为 null），AI 每日上限输入框必须显示 **200**（不是 2000）；
  2) 后端返回具体值时（如 50），输入框必须尊重后端返回值（显示 50）；
  3) 变异对照：把三处兜底 `?? 200` 改回 `?? 2000` → 输入框显示 2000 →
     上面的断言（期望 200）必然变红，证明探针有牙。
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
SETTINGS_JS = STATIC / "js" / "views" / "settings.js"
NODE = os.environ.get("NODE", "node")

HARNESS = r"""
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
const showToast = () => {};
const alert = () => {};
const setTimeout = () => 0;
const document = { createElement: () => ({ set innerHTML(_v) {}, appendChild() {} }), getElementById: () => null };
const location = { hash: '' };
`;

const exportsLine = `
return { render };
`;

function makeRoot() {
  let _html = '';
  const fakeEl = {
    addEventListener() {}, removeEventListener() {},
    getAttribute() { return null; }, setAttribute() {},
    querySelector() { return fakeEl; }, querySelectorAll() { return []; },
    classList: { add() {}, remove() {}, toggle() {} },
    style: {}, dataset: {}, textContent: '', value: '',
    appendChild() {}, remove() {}, focus() {},
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


def _run_settings(settings_source: str, fixtures: dict) -> dict:
    tmp = pathlib.Path(tempfile.mkdtemp())
    module_path = tmp / "settings_under_test.js"
    module_path.write_text(settings_source, encoding="utf-8")
    fixture_path = tmp / "fixture.json"
    fixture_path.write_text(json.dumps(fixtures, ensure_ascii=False), encoding="utf-8")
    harness_path = tmp / "harness.js"
    harness_path.write_text(HARNESS, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(harness_path), str(module_path), str(fixture_path)],
        text=True, encoding="utf-8", capture_output=True, check=False, timeout=90,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise AssertionError(
            f"node 执行失败 rc={result.returncode}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
    return json.loads(result.stdout)


def _ai_input_value(html: str):
    m = re.search(r'id="ai-daily-budget"[^>]*value="([^"]*)"', html)
    return m.group(1) if m else None


class SettingsDailyBudgetTests(unittest.TestCase):
    """真跑 settings.js，验证每日上限兜底跟随后端默认 200。"""

    def test_no_backend_value_shows_200_not_2000(self):
        """后端未返回值时，输入框必须显示 200（不是旧值 2000）。"""
        fixtures = {
            "/api/settings/backup": None,
            "/api/settings/retention": None,
            "/api/settings/learning": None,
            "/api/settings/forecast-archive": None,
            "/api/settings/ai": None,
            "/api/interests": {"objects": [], "links": []},
        }
        outcome = _run_settings(SETTINGS_JS.read_text(encoding="utf-8"), fixtures)
        self.assertTrue(
            outcome.get("ok"),
            f"渲染抛异常：{outcome.get('errorName')}: {outcome.get('error')}",
        )
        self.assertEqual(
            _ai_input_value(outcome["html"]), "200",
            "后端未返回值时每日上限兜底必须是 200，而不是旧值 2000",
        )

    def test_backend_value_is_respected(self):
        """后端返回具体值时，输入框必须尊重后端返回值。"""
        fixtures = {
            "/api/settings/backup": None,
            "/api/settings/retention": None,
            "/api/settings/learning": None,
            "/api/settings/forecast-archive": None,
            "/api/settings/ai": {"daily_budget": 50},
            "/api/interests": {"objects": [], "links": []},
        }
        outcome = _run_settings(SETTINGS_JS.read_text(encoding="utf-8"), fixtures)
        self.assertTrue(outcome.get("ok"), f"渲染抛异常：{outcome.get('error')}")
        self.assertEqual(
            _ai_input_value(outcome["html"]), "50",
            "后端返回 50 时输入框必须显示 50",
        )

    def test_mutation_control_fallback_2000_must_go_red(self):
        """把三处兜底 `?? 200` 改回 `?? 2000` → 输入框变 2000 → 上面的断言必然变红。"""
        source = SETTINGS_JS.read_text(encoding="utf-8")
        mutated = re.sub(r"\?\? 200\b", "?? 2000", source)
        self.assertNotEqual(mutated, source, "变异替换没生效，对照无效")

        fixtures = {
            "/api/settings/backup": None,
            "/api/settings/retention": None,
            "/api/settings/learning": None,
            "/api/settings/forecast-archive": None,
            "/api/settings/ai": None,
            "/api/interests": {"objects": [], "links": []},
        }
        outcome = _run_settings(mutated, fixtures)
        self.assertTrue(outcome.get("ok"), f"变异版渲染抛异常：{outcome.get('error')}")
        self.assertEqual(
            _ai_input_value(outcome["html"]), "2000",
            "兜底改回 2000 后输入框应显示 2000 —— 否则正常断言（期望 200）不会变红，探针没牙",
        )


if __name__ == "__main__":
    unittest.main()
