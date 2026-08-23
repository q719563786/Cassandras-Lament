// 远见 v1.0 · 今日远见 · 行动雷达模式
// 首页 = L4 立即行动 + L3 准备观察 + 今日低后悔动作 + 预测进度 KPI + 告诉远见
import { api, escapeHtml } from '../api.js';
import { tellBoxHtml, bindTellBox } from './tell.js';

// 方向文本 → CSS 颜色类（风险上升=红 / 风险缓解=绿 / 没有明显变化=灰）
function directionClass(direction) {
  if (direction === '风险上升') return 'up';
  if (direction === '风险缓解') return 'down';
  return 'flat';
}

// L4 立即行动卡：复选框 + title（利益名：事实摘要）+ time_window + action
function actionL4Html(item) {
  const title = escapeHtml(item.title || item.interest_name || '');
  const window = escapeHtml(item.time_window || '');
  const action = escapeHtml(item.action || item.advice || '');
  return `<label class="action-card">
    <div class="ac-title"><input type="checkbox" aria-label="完成 ${title}"> ${title}</div>
    <div class="ac-meta"><span>窗口：${window}</span></div>
    ${action ? `<div class="ac-action">${action}</div>` : ''}
  </label>`;
}

// L3 / 观察卡：title + time_window + direction（按 spec 不显示 action）
function watchCardHtml(item) {
  const title = escapeHtml(item.title || item.interest_name || '');
  const window = escapeHtml(item.time_window || '');
  const direction = item.direction || '没有明显变化';
  const dirClass = directionClass(direction);
  const cardClass = item.mode === 'watch' ? 'action-card watch' : 'action-card l3';
  return `<div class="${cardClass}">
    <div class="ac-title">${title}</div>
    <div class="ac-meta"><span>窗口：${window}</span><span class="ac-direction ${dirClass}">${escapeHtml(direction)}</span></div>
  </div>`;
}

// 今日低后悔动作区（绿色标记，固定 4 条）
function safeListHtml() {
  const items = [
    '保留票据和结算材料',
    '不把信用额度算作家庭资产',
    '新事实出现后更新预测，不覆盖旧版本',
    '暂缓不可逆决定，等更多信息'
  ];
  return `<section class="radar-section">
    <h2><span class="dot-safe"></span> 今日低后悔动作</h2>
    <ul class="safe-list">${items.map(t => `<li>${escapeHtml(t)}</li>`).join('')}</ul>
  </section>`;
}

// 预测进度 KPI：已结算 / 命中 / 失误 / 本周到期
function progressHtml(progress) {
  if (!progress) return '';
  const { resolved_total, hit_total, miss_total, due_this_week, overdue_total } = progress;
  const overdue = Number(overdue_total ?? 0);
  const overdueBanner = overdue > 0
    ? `<div class="overdue-banner"><span class="overdue-count">${overdue}</span> 条预测已到期待结算 — <a href="#/calib">去结算</a></div>`
    : '';
  return `<section class="card u-mb-md">
    ${overdueBanner}
    <div class="u-row">
      <div class="kpi-block"><div class="kpi-num">${String(resolved_total ?? 0)}</div><div class="kpi-label">已结算预测</div></div>
      <div class="kpi-block"><div class="kpi-num">${String(hit_total ?? 0)}</div><div class="kpi-label">历史命中</div></div>
      <div class="kpi-block"><div class="kpi-num">${String(miss_total ?? 0)}</div><div class="kpi-label">历史失误</div></div>
      <div class="kpi-block"><div class="kpi-num">${String(due_this_week ?? 0)}</div><div class="kpi-label">本周到期</div></div>
    </div>
  </section>`;
}

export async function render(root) {
  // 并行拉取风险面板 + 预测进度，各自独立失败降级为 null
  const [dashboard, progress] = await Promise.all([
    api('/api/risk-dashboard').catch(() => null),
    api('/api/forecasts/progress').catch(() => null)
  ]);

  const state = dashboard?.state || '';
  const items = Array.isArray(dashboard?.items) ? dashboard.items : [];
  const l4 = items.filter(i => i.mode === 'action' && i.alert_level === 'L4');
  const l3 = items.filter(i => (i.mode === 'action' && i.alert_level === 'L3') || i.mode === 'watch');

  let sections = '';

  // 状态分支：读不到 / stable / coverage_gap 显示对应空态；action / watch 渲染雷达
  if (!dashboard) {
    sections += `<div class="radar-empty">暂时读不到风险面板，请稍后重试。</div>`;
  } else if (state === 'stable') {
    sections += `<div class="radar-empty">目前没有需要你处理的高等级风险，系统仍在后台监控。</div>`;
  } else if (state === 'coverage_gap') {
    sections += `<div class="radar-empty">公开信息监控覆盖不足，系统正在重试。</div>`;
  } else {
    if (dashboard.summary) {
      sections += `<div class="radar-summary">${escapeHtml(dashboard.summary)}</div>`;
    }
    // L4 立即行动区（红）
    sections += `<section class="radar-section">
      <h2><span class="dot-l4"></span> L4 · 立即行动</h2>
      ${l4.length ? l4.map(actionL4Html).join('') : '<div class="radar-empty">今天没有 L4 等级的立即行动项。</div>'}
    </section>`;
    // L3 准备观察区（橙，含 mode=watch 项）
    sections += `<section class="radar-section">
      <h2><span class="dot-l3"></span> L3 · 准备观察</h2>
      ${l3.length ? l3.map(watchCardHtml).join('') : '<div class="radar-empty">暂无需要继续观察的事项。</div>'}
    </section>`;
  }

  // 今日低后悔动作区（绿，固定 4 条）
  sections += safeListHtml();

  root.innerHTML = `<div class="u-max">
    ${sections}
    ${progressHtml(progress)}
  </div>`;

  // 告诉远见折叠框
  const tellSlot = document.createElement('div');
  tellSlot.className = 'u-max u-mt-md';
  tellSlot.innerHTML = `<details class="tell card"><summary class="section-title">告诉远见 · 有新情况要记录</summary><div class="tell-slot u-mt-sm"></div></details>`;
  root.appendChild(tellSlot);
  const inlineSlot = tellSlot.querySelector('.tell-slot');
  if (inlineSlot) {
    inlineSlot.innerHTML = tellBoxHtml('inline');
    bindTellBox(inlineSlot);
  }
}
