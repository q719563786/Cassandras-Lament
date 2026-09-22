"""今日页（today.js）的**真执行**测试 —— 专门抓 N1 监听器泄漏。

为什么必须真执行：旧 `bindCardActions` 在每次 render 都往**持久节点** `#view-root`
（router.js:33）挂新的 click 监听，且挂了两路。今日页每 45s 自刷一次，监听随刷新
次数线性叠加，一次点击会重复触发（confirm-prob 重复 POST / 重复 renderView）。
这是"源码文本包含某些字符串"的断言绝对抓不到的运行时缺陷 —— 必须真把 today.js 拿到
node 里跑 3 次 render，再模拟一次点击，数清楚 action 被调用几次、节点上挂着几个监听。

本测试：
  1) 正常源码：连续 render 3 次后，模拟一次 confirm-prob 点击，断言
     POST 只发 1 次、#view-root 上只挂 1 个 click 监听；
  2) 变异对照：把"先移除再绑定"那一步去掉 → 监听叠加，一次点击会发多次 → 测试必须变红。
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
TODAY_JS = STATIC / "js" / "views" / "today.js"
NODE = os.environ.get("NODE", "node")

HARNESS = r"""
const fs = require('fs');
const modulePath = process.argv[2];
let src = fs.readFileSync(modulePath, 'utf8');
// 去掉 ES module 的 import / export，换成桩（其余视图函数只依赖这几个全局）
src = src.replace(/^import[^;]*;\s*$/mg, '');
src = src.replace(/^export\s+/gm, '');

const stubs = `
const apiCalls = [];
const api = async (url, opts) => { apiCalls.push(String(url)); return {}; };
const escapeHtml = (v) => String(v === null || v === undefined ? '' : v)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
const tellBoxHtml = () => '';
const bindTellBox = () => {};
const showLoading = () => {};
const showPageError = () => {};
const alert = () => {};
const setTimeout = () => 0;
const document = {
  createElement: () => ({
    set innerHTML(_v) {}, appendChild() {},
    firstElementChild: { remove() {} }, querySelector() { return null; },
  }),
};
const location = { hash: '' };
`;

const exportsLine = `
return { render, bindCardActions, apiCalls };
`;

// 以下两个构造函数在 node 顶层作用域（不在 new Function 内），供主逻辑使用
function makeRoot() {
  const listeners = [];
  return {
    _listeners: listeners,
    addEventListener(type, fn) { listeners.push({ type, fn }); },
    removeEventListener(type, fn) {
      const i = listeners.findIndex(l => l.type === type && l.fn === fn);
      if (i >= 0) listeners.splice(i, 1);
    },
    querySelector() { return null; },
    appendChild() {},
    classList: { add() {} },
    set innerHTML(_v) {},
    get innerHTML() { return ''; },
  };
}
function makeConfirmBtn() {
  const card = { dataset: { cluster: 'C-1', impact: 'P-9' }, classList: { add() {} }, remove() {} };
  const btn = {
    dataset: { action: 'confirm-prob', prob: '0.6', impact: 'P-9' },
    disabled: false, textContent: '确认',
    closest(sel) {
      if (sel === '.action-card') return card;
      if (sel === '.modal-backdrop') return null;
      if (sel === 'button[data-action]') return btn;
      return null;
    },
    matches() { return false; },
  };
  return btn;
}

const out = { ok: false };
(async () => {
  try {
    const factory = new Function(stubs + src + exportsLine);
    const mod = factory();
    const root = makeRoot();
    // 模拟今日页每 45s 自刷一次：连续 render 3 次
    await mod.render(root);
    await mod.render(root);
    await mod.render(root);
    out.listenerCount = root._listeners.filter(l => l.type === 'click').length;
    // 模拟一次点击（confirm-prob 按钮）
    const btn = makeConfirmBtn();
    const ev = { target: btn, stopPropagation() {} };
    const proms = [];
    for (const l of root._listeners) {
      if (l.type === 'click') proms.push(l.fn(ev));
    }
    await Promise.allSettled(proms);
    out.confirmCalls = mod.apiCalls.filter(u => /\/confirm/.test(u)).length;
    out.ok = true;
  } catch (error) {
    out.error = String(error && error.message ? error.message : error);
    out.errorName = String(error && error.name ? error.name : '');
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


def _run_today(source_text: str) -> dict:
    tmp = pathlib.Path(tempfile.mkdtemp())
    module_path = tmp / "today_under_test.js"
    module_path.write_text(source_text, encoding="utf-8")
    harness_path = tmp / "harness.js"
    harness_path.write_text(HARNESS, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(harness_path), str(module_path)],
        text=True, encoding="utf-8", capture_output=True, check=False, timeout=90,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise AssertionError(
            f"node 执行失败 rc={result.returncode}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
    return json.loads(result.stdout)


@unittest.skipUnless(os.name == "nt" or True, "需要 node")
class TodayListenerLeakTests(unittest.TestCase):
    """真跑 today.js，验证监听器不会随刷新叠加。"""

    def test_render_three_times_then_one_click_fires_action_once(self):
        outcome = _run_today(TODAY_JS.read_text(encoding="utf-8"))
        self.assertTrue(outcome.get("ok"), f"渲染抛异常：{outcome.get('errorName')}: {outcome.get('error')}")
        self.assertEqual(outcome["listenerCount"], 1, "持久节点上不应叠加 click 监听")
        self.assertEqual(outcome["confirmCalls"], 1, "一次点击不应重复 POST")

    def test_mutation_control_removing_unbind_step_must_go_red(self):
        """去掉"先移除再绑定"那一步 → 监听叠加 → 一次点击发多次 → 测试必须变红。"""
        source = TODAY_JS.read_text(encoding="utf-8")
        mutated = re.sub(
            r"  if \(root\._todayCardClick\) \{.*?root\.removeEventListener\('click', root\._todayCardClick\);.*?\}\n",
            "",
            source,
            flags=re.S,
        )
        self.assertNotEqual(mutated, source, "变异替换没生效，对照无效")

        outcome = _run_today(mutated)
        self.assertTrue(outcome.get("ok"), f"变异版渲染抛异常：{outcome.get('error')}")
        self.assertGreater(
            outcome["listenerCount"], 1,
            "去掉先移除那步后，监听应当叠加（>1），否则本探针没牙",
        )
        self.assertGreater(
            outcome["confirmCalls"], 1,
            "去掉先移除那步后，一次点击应重复触发 action（>1 次）",
        )


if __name__ == "__main__":
    unittest.main()
