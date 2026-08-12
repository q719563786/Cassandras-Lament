import subprocess
import unittest
from pathlib import Path


class CognitionFrontendTests(unittest.TestCase):
    module_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "yuanjian_app"
        / "static"
        / "cognition_ui.js"
    )
    ui_core_path = module_path.with_name("ui_core.js")
    risk_ui_path = module_path.with_name("risk_ui.js")

    def run_node(self, body):
        script = f"""
const assert = require('node:assert/strict');
const ui = require({str(self.module_path)!r});
(async () => {{
{body}
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

    def run_ui_core(self, body):
        script = f"""
const assert = require('node:assert/strict');
const ui = require({str(self.ui_core_path)!r});
{body}
"""
        result = subprocess.run(
            ["node", "-e", script],
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def run_risk_ui(self, body):
        script = f"""
const assert = require('node:assert/strict');
const ui = require({str(self.risk_ui_path)!r});
{body}
"""
        result = subprocess.run(
            ["node", "-e", script],
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_risk_cockpit_labels_and_workload_are_small_and_stable(self):
        self.run_risk_ui(
            """
assert.equal(ui.overviewLabel('action'), '需要行动');
assert.equal(ui.overviewLabel('watch'), '继续观察');
assert.equal(ui.overviewLabel('stable'), '目前平稳');
assert.equal(ui.overviewLabel('coverage_gap'), '监控覆盖不足');
assert.deepEqual(ui.counts({counts:{action:2, watch:3, verifying:9}}), [
  {key:'action', value:2, label:'现在要处理'},
  {key:'watch', value:3, label:'继续观察'},
  {key:'verifying', value:9, label:'系统核实中'}
]);
const items = [
  {cluster_id:'low', alert_level:'L2', mode:'action', impact_score:1},
  {cluster_id:'watch', alert_level:'L3', mode:'watch', impact_score:.9},
  {cluster_id:'a', alert_level:'L4', mode:'action', impact_score:.8},
  {cluster_id:'b', alert_level:'L3', mode:'action', impact_score:.7},
  {cluster_id:'c', alert_level:'L3', mode:'watch', impact_score:.6},
  {cluster_id:'d', alert_level:'L3', mode:'watch', impact_score:.5},
  {cluster_id:'e', alert_level:'L3', mode:'watch', impact_score:.4}
];
assert.deepEqual(ui.visibleRisks(items).map(item => item.cluster_id), ['a','b','watch']);
"""
        )

    def test_input_result_and_tutorial_state_use_plain_local_contracts(self):
        self.run_risk_ui(
            """
assert.deepEqual(ui.inputResult({alert_level:'L4', recommended_action:'马上联系医院'}), {
  advice:'马上联系医院', risk:'高风险'
});
assert.deepEqual(ui.inputResult({alert_level:'L3', recommended_action:'先保留现金'}), {
  advice:'先保留现金', risk:'中风险'
});
assert.deepEqual(ui.inputResult({alert_level:'L2', recommended_action:''}), {
  advice:'先记录事实，暂不做不可逆决定。', risk:'低风险'
});
const steps = ui.tutorialSteps();
assert.deepEqual(steps.map(item => item.title), [
  '先看行动首页', '把新情况告诉远见', '系统会继续盯着'
]);
const values = new Map();
const storage = {
  getItem:key => values.has(key) ? values.get(key) : null,
  setItem:(key, value) => values.set(key, value)
};
assert.equal(ui.tutorialSeen(storage, '0.9'), false);
assert.equal(ui.rememberTutorial(storage, '0.9'), true);
assert.equal(ui.tutorialSeen(storage, '0.9'), true);
const broken = {getItem:() => { throw new Error('blocked'); }, setItem:() => { throw new Error('blocked'); }};
assert.equal(ui.tutorialSeen(broken, '0.9'), false);
assert.equal(ui.rememberTutorial(broken, '0.9'), false);
"""
        )

    def test_risk_detail_puts_action_before_evidence_and_back_office_is_lazy(self):
        self.run_risk_ui(
            """
const detail = ui.detailSummary({
  judgment:{fact_summary:'政策正在执行', horizons:['7天内'], up_triggers:['扩大执行'], down_triggers:['暂停执行']},
  impacts:[{interest_name:'家庭现金流', reason:'可能增加支出', candidate:{recommended_action:'保留必要现金', window_end:'2026-08-19'}}]
});
assert.deepEqual(detail, {
  interest:'家庭现金流', impact:'可能增加支出', action:'保留必要现金',
  decisionBy:'2026-08-19', triggers:['风险升级：扩大执行','风险解除：暂停执行']
});
assert.deepEqual(ui.intelligenceRequests('summary'), ['/api/cognition/status','/api/external/sources']);
assert.deepEqual(ui.intelligenceRequests('raw'), ['/api/external/radar?limit=10&offset=0']);
assert.deepEqual(ui.intelligenceRequests('manage'), ['/api/external/sources','/api/external/rules']);
"""
        )

    def test_personal_view_uses_the_same_plain_label_as_navigation(self):
        app_script = self.module_path.with_name("app.js").read_text(encoding="utf-8")

        self.assertIn("setHeader('我的情况'", app_script)
        self.assertNotIn("setHeader('我的利益'", app_script)
        self.assertIn("subnav('我的情况'", app_script)

    def test_user_facing_labels_query_and_time_are_stable(self):
        self.run_ui_core(
            """
assert.equal(ui.evidenceLabel('E2'), 'E2 · 多源互证');
assert.equal(ui.trendLabel('low_sample'), '样本积累中');
assert.equal(ui.categoryLabel('employment'), '就业');
assert.equal(ui.categoryLabel('general'), '综合');
assert.equal(ui.sourceKindLabel('rss'), 'RSS / Atom');
assert.equal(ui.sourceKindLabel('html_list'), '公开网页列表');
assert.equal(ui.sourceKindLabel('gdelt'), 'GDELT 全球新闻索引');
assert.equal(ui.statusLabel('active'), '有效');
assert.equal(ui.statusLabel('error'), '待重试');
assert.equal(
  ui.buildQuery({limit:10, offset:0, q:'医保', evidence:''}),
  '?limit=10&offset=0&q=%E5%8C%BB%E4%BF%9D'
);
const now = new Date('2026-08-12T04:00:00Z');
assert.equal(ui.formatLocalTime('2026-08-12T01:20:00Z', now, 0), '今天 01:20');
assert.equal(ui.formatLocalTime('2026-08-11T03:10:00Z', now, 0), '昨天 03:10');
assert.equal(ui.formatLocalTime('', now, 0), '时间未知');
"""
        )

    def test_metric_filter_resets_pagination_and_maps_unread(self):
        self.run_ui_core(
            """
assert.deepEqual(
  ui.applyMetricFilter({limit:10, offset:30, q:'医保'}, 'unread'),
  {limit:10, offset:0, q:'医保', notification_status:'unread'}
);
assert.deepEqual(
  ui.applyMetricFilter({limit:10, offset:20, q:'医保'}, 'judge'),
  {limit:10, offset:0, q:'医保', needs_judgment:true}
);
"""
        )

    def test_pagination_and_source_health_keep_user_context(self):
        self.run_ui_core(
            """
assert.deepEqual(
  ui.movePage({limit:10, offset:10, q:'医保'}, 'next', 35),
  {limit:10, offset:20, q:'医保'}
);
assert.deepEqual(
  ui.movePage({limit:10, offset:0, q:'医保'}, 'prev', 35),
  {limit:10, offset:0, q:'医保'}
);
assert.equal(ui.sourceHealthLabel({enabled:false, last_status:'ok', stale:false}), '已暂停');
assert.equal(ui.sourceHealthLabel({enabled:true, last_status:'error', stale:false}), '待重试');
assert.equal(ui.sourceHealthLabel({enabled:true, last_status:'ok', stale:true}), '内容陈旧');
"""
        )

    def test_success_shows_busy_then_refreshes_with_a_specific_result(self):
        self.run_node(
            """
let finish;
const button = {disabled:false, textContent:'立即运行认知'};
const status = {textContent:'', className:''};
let notice;
const running = ui.runCognitionWithFeedback({
  apiCall: () => new Promise(resolve => { finish = resolve; }),
  button,
  status,
  onComplete: async value => { notice = value; },
  setIntervalFn: () => 7,
  clearIntervalFn: id => assert.equal(id, 7)
});
await Promise.resolve();
assert.equal(button.disabled, true);
assert.equal(button.textContent, '正在运行认知…');
assert.match(status.textContent, /正在聚合信息/);
finish({backfill:{processed:3}, queued:2, judgments:{succeeded:1}, mapped_impacts:4, notifications_created:1, elapsed_ms:5300});
await running;
assert.equal(button.disabled, false);
assert.equal(button.textContent, '立即运行认知');
assert.equal(notice.kind, 'success');
assert.match(notice.text, /处理3条信息/);
assert.match(notice.text, /形成4条利益影响/);
assert.equal(notice.text.includes('5.3秒'), true);
"""
        )

    def test_zero_result_and_error_are_both_visible_and_button_recovers(self):
        self.run_node(
            """
const makeElement = () => ({disabled:false, textContent:'立即运行认知', className:''});
let button = makeElement();
let status = makeElement();
let notice;
await ui.runCognitionWithFeedback({
  apiCall: async () => ({backfill:{processed:0}, queued:0, judgments:{succeeded:0}, mapped_impacts:0, notifications_created:0, elapsed_ms:9}),
  button, status, onComplete: async value => { notice = value; },
  setIntervalFn: () => 1, clearIntervalFn: () => {}
});
assert.equal(notice.text, '运行完成，本次没有新增待处理信息（0.0秒）');
button = makeElement(); status = makeElement(); let failure;
await ui.runCognitionWithFeedback({
  apiCall: async () => { throw new Error('认知任务正在运行，请稍候'); },
  button, status, onComplete: async () => {},
  onFailure: notice => { failure = notice; },
  setIntervalFn: () => 2, clearIntervalFn: () => {}
});
assert.equal(status.textContent, '运行失败：认知任务正在运行，请稍候');
assert.match(status.className, /error/);
assert.deepEqual(failure, {kind:'error', text:'运行失败：认知任务正在运行，请稍候'});
assert.equal(button.disabled, false);
assert.equal(button.textContent, '立即运行认知');
"""
        )


if __name__ == "__main__":
    unittest.main()
