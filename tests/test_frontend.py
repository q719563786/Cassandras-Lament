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
  '运行完成，本次没有新增待处理信息（0.0秒）'
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


if __name__ == "__main__":
    unittest.main()
