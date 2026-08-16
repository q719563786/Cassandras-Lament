// 远见 v0.9 · 今日远见 —— 单句英雄区 + 内联告诉远见折叠框 + 覆盖不足空态（AC-01）
import { api, escapeHtml, showPageError } from '../api.js';
import { yjIcon } from '../icons.js';
import { riskTag, categoryLabel, formatLocalTime } from '../ui_core.js';
import { tellBoxHtml, bindTellBox } from './tell.js';

// 从 risk-dashboard 取最高优先级一条（mode action 优先 → L4 → impact_score）
function topRisk(dashboard) {
  const items = Array.isArray(dashboard?.items) ? dashboard.items : [];
  const visible = items.filter(item => item && (item.alert_level === 'L3' || item.alert_level === 'L4'));
  if (!visible.length) return null;
  return visible.sort((left, right) => {
    const mode = Number(right.mode === 'action') - Number(left.mode === 'action');
    if (mode) return mode;
    const level = Number(right.alert_level === 'L4') - Number(left.alert_level === 'L4');
    if (level) return level;
    return (Number(right.impact_score) || 0) - (Number(left.impact_score) || 0);
  })[0];
}

function heroHtml(item) {
  const action = item.recommended_action || item.advice || '暂无建议，保持观察。';
  const meta = [
    item.category ? categoryLabel(item.category) : '',
    item.evidence ? `证据 ${item.evidence}` : '',
    item.updated_at ? formatLocalTime(item.updated_at) : ''
  ].filter(Boolean).join(' · ');
  const why = Array.isArray(item.reasons) && item.reasons.length
    ? `<details class="why"><summary>为何是这个判断</summary><ul class="body">${item.reasons.map(r => `<li>${escapeHtml(r)}</li>`).join('')}</ul></details>`
    : '';
  return `<section class="today-hero u-max">
    <p class="oracle"><span class="prompt" aria-hidden="true">❯</span><span class="body">${escapeHtml(action)}</span></p>
    <div class="today-meta">${riskTag(item.alert_level)}<span>${escapeHtml(meta || '个人利益雷达')}</span></div>
    ${why}
  </section>
  <section class="u-max u-mt-md">
    <details class="tell card">
      <summary class="section-title">告诉远见 · 有新情况要记录</summary>
      <div class="tell-slot u-mt-sm"></div>
    </details>
  </section>`;
}

const EMPTY_HTML = `<div class="empty u-max">
  ${yjIcon('ic_offline', 24, '暂无信号')}
  <p>监控覆盖不足——先去源管理启用几个与你相关的信息源，或把新情况直接告诉远见。</p>
  <div class="u-row" data-go>
    <button type="button" class="btn btn-sm" data-go-view="sources">前往源管理</button>
    <button type="button" class="btn btn-sm btn-primary" data-go-view="tell">告诉远见</button>
  </div>
</div>`;

export async function render(root) {
  let dashboard = null;
  try {
    dashboard = await api('/api/risk-dashboard?limit=5');
  } catch (error) {
    showPageError(root, `风险面板读取失败：${error.message}`, () => render(root));
    return;
  }
  // 覆盖不足空态沿用：dashboard state 为 coverage_gap 或无可见风险时明示，不渲染假数据
  if (dashboard?.state === 'coverage_gap') {
    root.innerHTML = `<div class="note u-max u-mb-md">${escapeHtml(dashboard?.summary || '监控覆盖不足——启用的信息源太少，暂时无法形成可靠判断。')}</div>${EMPTY_HTML}`;
  } else {
    const item = topRisk(dashboard);
    root.innerHTML = item ? heroHtml(item) : EMPTY_HTML;
  }
  // 内联告诉远见折叠框（复用 tell 模块）
  const slot = root.querySelector('.tell-slot');
  if (slot) {
    slot.innerHTML = tellBoxHtml('inline');
    bindTellBox(slot);
  }
  // data-go 跳转链：直接改 hash，避免 router ↔ views 循环依赖
  root.querySelectorAll('[data-go-view]').forEach(btn => {
    btn.addEventListener('click', () => { location.hash = `#/${btn.dataset.goView}`; });
  });
}
