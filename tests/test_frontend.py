import json
import subprocess
import unittest
from pathlib import Path
from urllib.request import pathname2url


STATIC = Path(__file__).resolve().parents[1] / "src" / "yuanjian_app" / "static"


def file_url(path: Path) -> str:
    return "file:" + pathname2url(str(path))


class TerminalFrontendTests(unittest.TestCase):
    """新终端 UI（ES modules）的前端契约测试。

    ui_core.js 零 DOM 依赖，用 node 动态 import 实测纯函数；
    其余视图模块顶层引用 location/document，node 无法加载，
    改为对源码文本做结构性契约断言。
    """

    ui_core = STATIC / "js" / "ui_core.js"

    def run_ui_core(self, body):
        script = f"""
const assert = require('node:assert/strict');
(async () => {{
  const ui = await import({file_url(self.ui_core)!r});
  {{
{body}
  }}
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
        result = subprocess.run(
            ["node", "-e", script],
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_label_helpers_are_plain_and_stable(self):
        self.run_ui_core(
            """
assert.equal(ui.evidenceLabel('E2'), 'E2 · 多源互证');
assert.equal(ui.evidenceLabel(''), '证据待确认');
assert.equal(ui.trendLabel('low_sample'), '样本积累中');
assert.equal(ui.categoryLabel('employment'), '就业');
assert.equal(ui.categoryLabel('general'), '综合');
assert.equal(ui.categoryLabel('nosuch'), 'nosuch');
assert.equal(ui.sourceKindLabel('rss'), 'RSS / Atom');
assert.equal(ui.sourceKindLabel('html_list'), '公开网页列表');
assert.equal(ui.sourceKindLabel('gdelt'), 'GDELT 全球新闻索引');
assert.equal(ui.sourceKindLabel('json_api'), '公开数据接口');
assert.equal(ui.statusLabel('active'), '有效');
assert.equal(ui.statusLabel('error'), '待重试');
assert.equal(ui.regionLabel('heyuan'), '河源');
assert.equal(ui.regionLabel('guangdong'), '广东');
assert.equal(ui.regionLabel('national'), '全国');
assert.equal(ui.regionLabel('global'), '全球');
assert.equal(ui.regionLabel(''), '未分区');
"""
        )

    def test_risk_mapping_keeps_three_user_facing_levels(self):
        self.run_ui_core(
            """
assert.equal(ui.riskLabel('L4'), '高风险');
assert.equal(ui.riskLabel('L3'), '中风险');
assert.equal(ui.riskLabel('L2'), '低风险');
assert.equal(ui.riskLabel('L1'), '低风险');
assert.equal(ui.riskClass('L4'), 'high');
assert.equal(ui.riskClass('L3'), 'mid');
assert.equal(ui.riskClass('L2'), 'low');
assert.equal(ui.riskTag('L4'), '<span class="tag tag-high">HIGH</span>');
assert.equal(ui.riskTag('L3'), '<span class="tag tag-mid">MID</span>');
assert.equal(ui.riskTag('L1'), '<span class="tag tag-low">LOW</span>');
"""
        )

    def test_query_paging_and_time_keep_user_context(self):
        self.run_ui_core(
            """
assert.equal(
  ui.buildQuery({limit: 10, offset: 0, q: '医保', evidence: ''}),
  '?limit=10&offset=0&q=%E5%8C%BB%E4%BF%9D'
);
assert.equal(ui.buildQuery({}), '');
assert.deepEqual(ui.pageRange(35, 10, 10), {start: 11, end: 20, total: 35});
assert.deepEqual(ui.pageRange(0, 10, 0), {start: 0, end: 0, total: 0});
assert.deepEqual(ui.movePage({limit: 10, offset: 10, q: '医保'}, 'next', 35), {limit: 10, offset: 20, q: '医保'});
assert.deepEqual(ui.movePage({limit: 10, offset: 0, q: '医保'}, 'prev', 35), {limit: 10, offset: 0, q: '医保'});
assert.deepEqual(ui.movePage({limit: 10, offset: 30, q: '医保'}, 'next', 35), {limit: 10, offset: 30, q: '医保'});
const now = new Date('2026-08-12T04:00:00Z');
assert.equal(ui.formatLocalTime('2026-08-12T01:20:00Z', now, 0), '今天 01:20');
assert.equal(ui.formatLocalTime('2026-08-11T03:10:00Z', now, 0), '昨天 03:10');
assert.equal(ui.formatLocalTime('', now, 0), '时间未知');
assert.equal(ui.formatLocalTime('not-a-date', now, 0), '时间未知');
"""
        )

    def test_source_health_and_run_summary_and_input_result(self):
        self.run_ui_core(
            """
assert.equal(ui.sourceHealthLabel({enabled: false, last_status: 'ok', stale: false}), '已暂停');
assert.equal(ui.sourceHealthLabel({enabled: true, last_status: 'error', stale: false}), '待重试');
assert.equal(ui.sourceHealthLabel({enabled: true, last_status: 'ok', stale: true}), '内容陈旧');
assert.equal(ui.sourceHealthLabel({enabled: true, last_status: 'ok', stale: false}), '正常');
assert.equal(
  ui.summarizeRun({backfill: {processed: 3}, queued: 2, judgments: {succeeded: 1}, mapped_impacts: 4, notifications_created: 1, elapsed_ms: 5300}),
  '运行完成：处理3条信息，排队2个事件，完成1次研判，形成4条利益影响，新增1条提醒（5.3秒）'
);
assert.equal(
  ui.summarizeRun({backfill: {processed: 0}, queued: 0, judgments: {succeeded: 0}, mapped_impacts: 0, notifications_created: 0, elapsed_ms: 9}),
  '运行完成（0.0秒）。尚无新增待处理信息——去源管理启用几个与你相关的信息源，或把新情况直接告诉远见。'
);
assert.deepEqual(ui.inputResult({alert_level: 'L4', recommended_action: '马上联系医院'}), {advice: '马上联系医院', risk: '高风险'});
assert.deepEqual(ui.inputResult({alert_level: 'L3', recommended_action: '先保留现金'}), {advice: '先保留现金', risk: '中风险'});
assert.deepEqual(ui.inputResult({alert_level: 'L2', recommended_action: ''}), {advice: '先记录事实，暂不做不可逆决定。', risk: '低风险'});
assert.equal(ui.formatBytes(512), '512 B');
assert.equal(ui.formatBytes(2048), '2.0 KB');
assert.equal(ui.formatBytes(5 * 1024 * 1024), '5.0 MB');
assert.equal(ui.formatBytes(-1), '未知');
"""
        )

    def test_views_use_only_local_module_contracts(self):
        app = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn("/api/cognition/run", app)
        self.assertIn("/api/shutdown", app)
        self.assertNotIn('src="https://', app)
        router = (STATIC / "js" / "router.js").read_text(encoding="utf-8")
        for view in ("today", "tell", "calib", "sources", "diag", "settings"):
            self.assertIn(view, router)

    def test_api_module_speaks_the_flat_json_contract(self):
        api_source = (STATIC / "js" / "api.js").read_text(encoding="utf-8")
        self.assertIn("X-YuanJian-Token", api_source)
        self.assertIn("error?.message", api_source)
        # token 从 URL 注入（pywebview 打开方式），永不写死。
        self.assertNotIn("token = '", api_source)

    def test_sources_view_supports_full_crud_and_opml(self):
        view = (STATIC / "js" / "views" / "sources.js").read_text(encoding="utf-8")
        self.assertIn("import-opml", view)
        self.assertIn("method: 'DELETE'", view)
        form = (STATIC / "js" / "views" / "sources-form.js").read_text(encoding="utf-8")
        self.assertIn("method: 'PUT'", form)
        self.assertIn("method: 'POST'", form)
        # json_api 源需要 config（items_path/fields），表单无配置入口，
        # 只由预置源使用——表单 KINDS 不对普通用户开放 json_api。
        kinds_block = form.split("const KINDS")[1].split("];")[0]
        self.assertIn("rss", kinds_block)
        self.assertIn("html_list", kinds_block)
        self.assertIn("gdelt", kinds_block)
        categories_block = form.split("const CATEGORIES")[1].split("];")[0]
        for category in ("gov", "water", "housing", "procurement", "news"):
            self.assertIn(category, categories_block)

    def test_settings_view_persists_via_put_and_exports_summary(self):
        view = (STATIC / "js" / "views" / "settings.js").read_text(encoding="utf-8")
        self.assertIn("'/api/settings/backup'", view)
        self.assertIn("'/api/settings/retention'", view)
        self.assertIn("'/api/settings/learning'", view)
        self.assertIn("method: 'PUT'", view)
        self.assertIn("/api/export/mobile-summary", view)

    def test_calibration_ledger_shows_percentages_correctly(self):
        view = (STATIC / "js" / "views" / "calib.js").read_text(encoding="utf-8")
        # 概率是 0-1 小数，显示必须乘 100（回归守护）。
        self.assertIn("(Number(f.probability) * 100).toFixed(0)", view)
        self.assertIn("/api/calibration", view)

    def test_calibration_confirm_button_is_wired(self):
        view = (STATIC / "js" / "views" / "calib.js").read_text(encoding="utf-8")
        # 回归守护 A2：候选 div 必须带 data-id，且绑定用 .candidate[data-id]，
        # 否则确认按钮点击无反应（旧 bug：选择器 .candidate[data-confirm]
        # 恒匹配空——data-confirm 在内层按钮上，不在 candidate div 上）。
        self.assertIn('class="candidate" data-id="', view)
        self.assertIn(".candidate[data-id]", view)
        # 最强回退守护：旧的错误选择器一旦复活，测试必失败。
        self.assertNotIn(".candidate[data-confirm]", view)
        # 确认时概率必须 /100 转成 0-1 比率（对齐后端白名单），否则被 400 拒绝。
        self.assertIn("Number(row.querySelector('select').value) / 100", view)

    def test_calibration_view_explains_every_metric_to_a_non_technical_user(self):
        """Regression: the calibration page used to ship four unexplained
        KPI cards (命中率 /误报率 /Brier /已结算预测) and a candidate list
        whose percentage dropdowns had no tooltip — non-technical users
        had to guess. The page now embeds a help panel that defines each
        term and explains the percentage dropdown is the user's own
        subjective probability."""
        view = (STATIC / "js" / "views" / "calib.js").read_text(encoding="utf-8")
        for term in ("命中率", "误报率", "Brier 分数", "已结算预测"):
            self.assertIn(f"<strong>{term}</strong>", view, f"missing explanation for {term}")
        # The percentage dropdown must explain what the number represents.
        self.assertIn("你判断这件事发生的概率", view)
        self.assertIn("主观概率", view)
        # The whole help panel must be in a <details> so it doesn't crowd
        # the dashboard on every visit.
        self.assertIn("<details class=\"card calib-help", view)

    def test_today_view_consumes_backend_gyw_and_falls_back_to_ui_template(self):
        """The Action Home deep-dive card must prefer the gyw sub-structure
        generated by LocalHeuristicProvider (and stored on the candidate
        via impacts.pending_candidates) over the UI fallback templates.
        It also must declare which source it used so the user can tell
        real analysis from template."""
        view = (STATIC / "js" / "views" / "today.js").read_text(encoding="utf-8")
        # Backend-first: read candidate.gyw before falling back.
        self.assertIn("candidate?.gyw", view)
        # All five GYW fields must be rendered (matching backend schema).
        for field in (
            "stakeholders",
            "constraints",
            "least_resistance_path",
            "counter_evidence",
            "leading_indicators",
        ):
            self.assertIn(field, view, f"today.js missing GYW field {field}")
        # The UI must label the source so the user knows whether they are
        # seeing real backend analysis or the UI fallback template.
        self.assertIn("后端 judgment 引擎", view)
        self.assertIn("UI 兜底模板", view)
        # The page must surface fact_summary and actors too, not just the
        # framework slots — otherwise the user sees analysis with no event.
        self.assertIn("fact_summary", view)
        self.assertIn("actors", view)


# ---------------------------------------------------------------------------
# 真实执行测试：脚本 + DOM stub + 真实 import + 真实 render(root)
# 之前只跑 grep 文本契约，蒙混了 .casefold() 这种 Python 习语移植。
# 现在用 node + 最小 DOM stub 真的 import 每个视图并调用一次 render，
# runtime 错误（不存在的方法 / 引用未定义）会在这里炸出来。
# ---------------------------------------------------------------------------


# Canned responses for the URLs the views fetch on first render. Each
# view has its own endpoint list; the script picks the first match.
CANNED_RESPONSES = [
    ("/api/forecasts/progress", json.dumps({
        "resolved_total": 0, "hit_total": 0,
        "miss_total": 0, "due_this_week": 0,
    })),
    ("/api/cognition/candidates", json.dumps({"candidates": [
        {
            "id": "P-test",
            "statement": "广东省最低工资标准调整可能在观察期内影响工作收入",
            "summary": "广东省最低工资标准调整",
            "category": "work",
            "window_end": "2026-09-15",
            "cluster_id": "C-1",
            "judgment_id": "J-1",
            "gyw": {
                "stakeholders": "推动方：广东省人社厅；阻力方：企业雇主",
                "constraints": "成本约束：企业利润空间",
                "least_resistance_path": "最小阻力路径：分阶段执行",
                "counter_evidence": "反对证据：经济下行",
                "leading_indicators": "领先指标：地方实施细则",
            },
            "fact_summary": "广东省最低工资标准调整通知",
            "actors": ["广东省人社厅"],
            "causal_chain": ["政策发布", "执行落地", "工资变化"],
        },
    ]})),
    ("/api/risk-dashboard", json.dumps({"state": "stable", "items": []})),
    ("/api/calibration", json.dumps({"hit_rate": None, "false_positive_rate": None, "brier": None, "resolved_total": 0, "brier_series": [], "by_category": {}, "candidates": []})),
    ("/api/diagnostics", json.dumps({"tiles": []})),
    ("/api/external/sources", json.dumps({"sources": []})),
    ("/api/external/rules", json.dumps({"rules": []})),
    ("/api/interests", json.dumps({"objects": [], "links": []})),
    ("/api/settings", json.dumps({"settings": {}})),
    ("/api/notifications", json.dumps({"items": [], "total": 0, "limit": 20, "offset": 0})),
    ("/api/events", json.dumps({"items": []})),
    ("/api/cognition/clusters", json.dumps({"items": [], "total": 0, "limit": 10, "offset": 0, "clusters": []})),
    ("/api/cognition/status", json.dumps({"running": False, "clusters": 0, "judgments": 0, "open_jobs": 0, "open_impacts": 0})),
    ("/api/forecasts", json.dumps({"forecasts": [], "total": 0})),
    ("/api/export/mobile-summary", json.dumps({"summary": "测试摘要"})),
]


def _build_view_runner_script(view_name, view_path):
    """Return a Node script that stubs the browser, imports the view
    module, and calls render(root). Catches runtime errors like the
    String.prototype.casefold() bug that would otherwise ship to users.
    """
    view_url = "file:" + pathname2url(str(view_path))
    # The stub has to cover everything the view touches at render time
    # without trying to emulate a real DOM. Anything the view references
    # that we don't stub → TypeError → test fails with a clear message.
    return f"""
const assert = require('node:assert/strict');
const {{ pathToFileURL }} = require('node:url');
const path = require('node:path');
const viewUrl = '{view_url}';

// ---- minimal browser stub ----
class FakeElement {{
  constructor(tag) {{
    this.tag = tag || 'div';
    this.children = [];
    this.attrs = {{}};
    this.dataset = {{}};
    this.style = {{}};
    this.classList = new Set();
    this.innerHTML = '';
    this.textContent = '';
    this.hidden = false;
    this.value = '';
  }}
  appendChild(c) {{ this.children.push(c); return c; }}
  removeChild(c) {{ this.children = this.children.filter(x => x !== c); }}
  setAttribute(k, v) {{ this.attrs[k] = String(v); }}
  addEventListener() {{ return null; }}
  removeEventListener() {{ return null; }}
  querySelector() {{ return new FakeElement(); }}
  querySelectorAll() {{ return []; }}
  getElementsByTagName() {{ return []; }}
  click() {{ return null; }}
}}
class FakeLocation {{
  constructor() {{ this.href = 'http://localhost/'; this.search = ''; this.hash = '#/today'; }}
}}
class FakeDocument {{
  constructor() {{ this.body = new FakeElement('body'); this._byId = {{}}; }}
  createElement(tag) {{ return new FakeElement(tag); }}
  getElementById(id) {{
    if (!this._byId[id]) this._byId[id] = new FakeElement('div');
    return this._byId[id];
  }}
  querySelector() {{ return new FakeElement(); }}
  querySelectorAll() {{ return []; }}
  addEventListener() {{ return null; }}
}}
class FakeMutationObserver {{ constructor() {{ }} observe() {{ }} disconnect() {{ }} }}
class FakeFile {{
  constructor(name, content) {{ this.name = name; this._c = content; }}
  async text() {{ return this._c; }}
}}
class FakeResponse {{
  constructor(ok, body) {{ this.ok = ok; this._body = body; this.status = ok ? 200 : 500; }}
  async text() {{ return this._body; }}
}}
const canned = {json.dumps(CANNED_RESPONSES)};
function cannedFor(url) {{
  for (const [pattern, body] of canned) if (url.includes(pattern)) return body;
  return 'null';
}}
const fetchLog = [];
async function fakeFetch(url, options) {{
  fetchLog.push({{ url, options }});
  return new FakeResponse(true, cannedFor(url));
}}

globalThis.window = {{ location: new FakeLocation() }};
globalThis.location = globalThis.window.location;
globalThis.document = new FakeDocument();
globalThis.MutationObserver = FakeMutationObserver;
globalThis.File = FakeFile;
globalThis.HTMLElement = FakeElement;
globalThis.Node = class {{ }};
globalThis.fetch = fakeFetch;
globalThis.localStorage = {{ getItem: () => null, setItem: () => null, removeItem: () => null }};
globalThis.requestAnimationFrame = (cb) => setTimeout(cb, 0);
globalThis.cancelAnimationFrame = (h) => clearTimeout(h);

(async () => {{
  const mod = await import(viewUrl);
  const root = document.getElementById('view-root');
  await mod.render(root);
  console.log('RENDER_OK');
}})().catch(error => {{
  console.error('RENDER_FAIL', error && error.stack || String(error));
  process.exit(1);
}});
"""


class BrowserViewRenderTests(unittest.TestCase):
    """Real JS execution: import each view module and call render(root).

    Catches the class of bugs the text-contract tests cannot — runtime
    errors like calling a non-existent string method (the .casefold()
    Pythonism that broke the Action Home on first launch).
    """

    JS_VIEWS = (
        "today", "calib", "sources", "diag", "settings", "tell",
    )

    def _run_view(self, view_name):
        view_path = STATIC / "js" / "views" / (view_name + ".js")
        script = _build_view_runner_script(view_name, view_path)
        result = subprocess.run(
            ["node", "-e", script],
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
            timeout=30,
        )
        return result

    def test_every_view_renders_without_runtime_error(self):
        """Regression: today.js used String.prototype.casefold() — a Python
        method, not a JS one. The text-contract test missed it. This test
        actually imports the module under a minimal DOM stub and calls
        render(root), so any future Pythonism-translation bug surfaces
        here in CI instead of on a user's desktop."""
        for view in self.JS_VIEWS:
            with self.subTest(view=view):
                result = self._run_view(view)
                self.assertEqual(
                    result.returncode, 0,
                    f"view '{view}' failed to render: stdout={result.stdout!r} stderr={result.stderr!r}",
                )
                self.assertIn("RENDER_OK", result.stdout, result.stderr)

    def test_no_pythonism_in_static_js(self):
        """The bug that caused today's crash: .casefold() is Python-only.
        Scan every JS file under static/ for it (and any future
        Pythonic idiom that would silently break in V8)."""
        offenders = []
        for path in STATIC.rglob("*.js"):
            text = path.read_text(encoding="utf-8")
            for line_num, line in enumerate(text.splitlines(), 1):
                if ".casefold(" in line:
                    offenders.append(f"{path.relative_to(STATIC)}:{line_num}: {line.strip()}")
        self.assertEqual(
            offenders, [],
            f"Python-only methods found in JS (not supported by V8/Node/browsers):\n"
            + "\n".join(offenders),
        )


def _build_router_runner_script(hash_route):
    """Render a hash route through the actual router module end-to-end.

    Catches problems that single-view tests cannot — e.g. a renderView()
    call that fails when two views try to share the same root element,
    or a route that imports something missing.
    """
    router_url = "file:" + pathname2url(str(STATIC / "js" / "router.js"))
    app_url = "file:" + pathname2url(str(STATIC / "js" / "app.js"))
    return f"""
class FakeElement {{
  constructor(tag) {{ this.tag = tag||'div'; this.children = []; this.attrs = {{}}; this.dataset = {{}}; this.style = {{}}; this.classList = new Set(); this.innerHTML = ''; this.textContent = ''; this.hidden = false; this.value = ''; }}
  appendChild(c) {{ this.children.push(c); return c; }}
  removeChild(c) {{ this.children = this.children.filter(x=>x!==c); }}
  setAttribute(k,v) {{ this.attrs[k]=String(v); }}
  addEventListener(ev,cb) {{ this._cb = this._cb || {{}}; this._cb[ev] = cb; }}
  removeEventListener() {{ }}
  querySelector() {{ return new FakeElement(); }}
  querySelectorAll(sel) {{
    if (sel && sel.startsWith('.nav-item[data-view]')) {{
      return ROUTES.map(name => {{
        const el = new FakeElement('button');
        el.dataset = {{ view: name, label: name }};
        return el;
      }});
    }}
    return [];
  }}
  getElementsByTagName() {{ return []; }}
  click() {{ }}
}}
class FakeLocation {{
  constructor(h) {{ this._hash = h || '#/today'; }}
  get hash() {{ return this._hash; }}
  set hash(v) {{ this._hash = v; if (this._onchange) this._onchange(); }}
  set onchange(cb) {{ this._onchange = cb; }}
  set href(v) {{ this._hash = v; if (this._onchange) this._onchange(); }}
  get href() {{ return 'http://localhost/' + this._hash; }}
  get search() {{ return ''; }}
  get pathname() {{ return '/'; }}
}}
class FakeDocument {{
  constructor() {{ this.body = new FakeElement('body'); this._byId = {{}}; }}
  createElement(tag) {{ return new FakeElement(tag); }}
  getElementById(id) {{ if (!this._byId[id]) this._byId[id] = new FakeElement('div'); return this._byId[id]; }}
  querySelector() {{ return new FakeElement(); }}
  querySelectorAll() {{ return []; }}
  addEventListener() {{ }}
}}
class FakeMutationObserver {{ constructor(){{}} observe(){{}} disconnect(){{}} }}
class FakeFile {{ constructor(n,c){{this.name=n;this._c=c;}} async text(){{return this._c;}} }}
class FakeResponse {{ constructor(ok,b){{this.ok=ok;this._b=b;this.status=ok?200:500;}} async text(){{return this._b;}} }}
const ROUTES = ['today','tell','calib','sources','diag','settings'];
const canned = {json.dumps(CANNED_RESPONSES)};
function cannedFor(url) {{
  for (const [pattern, body] of canned) if (url.includes(pattern)) return body;
  return 'null';
}}
async function fakeFetch(url, options) {{ return new FakeResponse(true, cannedFor(url)); }}

globalThis.window = {{
  location: new FakeLocation({json.dumps(hash_route)}),
  addEventListener(ev, cb) {{ if (ev === 'hashchange') this._hashcb = cb; }},
}};
globalThis.location = globalThis.window.location;
globalThis.document = new FakeDocument();
globalThis.MutationObserver = FakeMutationObserver;
globalThis.File = FakeFile;
globalThis.HTMLElement = FakeElement;
globalThis.Node = class {{ }};
globalThis.fetch = fakeFetch;
globalThis.localStorage = {{ getItem: () => null, setItem: () => null, removeItem: () => null }};
globalThis.requestAnimationFrame = (cb) => setTimeout(cb, 0);
globalThis.cancelAnimationFrame = (h) => clearTimeout(h);
globalThis.addEventListener = (ev, cb) => {{ if (ev === 'hashchange') globalThis.window._hashcb = cb; }};

(async () => {{
  const router = await import('{router_url}');
  await router.renderView();
  // Also try to render the next route by flipping the hash and dispatching.
  for (const next of ['tell', 'calib', 'sources', 'diag', 'settings']) {{
    globalThis.location.hash = '#/' + next;
    if (globalThis.window._hashcb) globalThis.window._hashcb();
    await router.renderView();
  }}
  console.log('ROUTER_OK');
}})().catch(error => {{
  console.error('ROUTER_FAIL', error && error.stack || String(error));
  process.exit(1);
}});
"""


class RouterIntegrationTests(unittest.TestCase):
    """End-to-end: import router.js, exercise every hash route through
    renderView(). Catches cross-view problems — a view that fails to
    import, a renderView() that fails when the root is reused, etc."""

    def test_every_hash_route_renders_via_router(self):
        script = _build_router_runner_script("#/today")
        result = subprocess.run(
            ["node", "-e", script],
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(
            result.returncode, 0,
            f"router end-to-end failed: stdout={result.stdout!r} stderr={result.stderr!r}",
        )
        self.assertIn("ROUTER_OK", result.stdout, result.stderr)


if __name__ == "__main__":
    unittest.main()
