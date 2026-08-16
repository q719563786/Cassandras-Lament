// 远见 v0.9 · 今日远见 —— 候选预测（具体预知）+ 最高优先级提醒 + 告诉远见（AC-01）
import { api, escapeHtml, showPageError, showToast, withBusy } from '../api.js';
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

// 把 risk_dashboard 的 item 翻译成"具体预知"，而不是通用"先核实..."模板。
// 用户要看的是"这件事是什么 + 截止日期 + 具体建议"，不是空话。
function heroHtml(item) {
  const title = item.title || item.reason || '外部事件需要关注';
  const advice = item.advice || item.recommended_action || '查看详情并按时间窗口行动。';
  const meta = [
    item.interest_name ? String(item.interest_name) : '',
    item.category ? categoryLabel(item.category) : '',
    item.time_window ? `时间窗口：${item.time_window}` : '',
    item.confidence ? `置信：${item.confidence}` : '',
    item.updated_at ? formatLocalTime(item.updated_at) : '',
  ].filter(Boolean).join(' · ');
  return `<section class="today-hero u-max">
    <p class="today-title">${escapeHtml(title)}</p>
    <p class="oracle"><span class="prompt" aria-hidden="true">❯</span><span class="body">${escapeHtml(advice)}</span></p>
    <div class="today-meta">${riskTag(item.alert_level)}<span>${escapeHtml(meta || '个人利益雷达')}</span></div>
  </section>`;
}

// 候选预测卡片：这是 user 在校准面板才能看到的"具体预知"列表。
// 把它直接放首页，用户双击 exe 立刻看到 5 条具体事件 + 选概率确认。
const PROBS = [90, 80, 70, 60, 50, 40, 30, 20, 10];
function candidatesHtml(candidates) {
  const list = Array.isArray(candidates) ? candidates : [];
  if (!list.length) return '';
  return `<h2 class="section-title u-mt-md">待确认预测（对你具体的预知）</h2>
  <div class="card">${list.map(c => `<div class="candidate" data-id="${escapeHtml(c.id)}">
    <div class="u-flex1"><p>${escapeHtml(c.statement || c.summary || '候选预测')}</p>
    <p class="u-dim">${escapeHtml(categoryLabel(c.category))} · 截止 ${escapeHtml(formatLocalTime(c.window_end))}</p></div>
    <div class="u-row"><select aria-label="你判断这件事发生的概率" title="选完后点确认，远见会在截止日检查并记入 Brier 分数。">
      ${PROBS.map(p => `<option value="${p}">${p}%</option>`).join('')}
    </select>
    <button type="button" class="btn btn-sm btn-primary" data-confirm>确认</button></div>
  </div>`).join('')}</div>`;
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
  let candidates = [];
  try {
    dashboard = await api('/api/risk-dashboard?limit=5');
  } catch (error) {
    showPageError(root, `风险面板读取失败：${error.message}`, () => render(root));
    return;
  }
  // 候选预测是首页的核心内容——具体事件标题 + 截止日期 + 选概率。
  // 单独拉，不依赖 risk_dashboard 是否返回。
  try {
    const response = await api('/api/cognition/candidates');
    candidates = Array.isArray(response?.candidates) ? response.candidates
      : Array.isArray(response) ? response : [];
  } catch (_) { candidates = []; }

  const item = topRisk(dashboard);
  const header = item ? heroHtml(item) : '';
  const candidatesBlock = candidatesHtml(candidates);

  if (dashboard?.state === 'coverage_gap' && !item && !candidatesBlock) {
    root.innerHTML = `<div class="note u-max u-mb-md">${escapeHtml(dashboard?.summary || '监控覆盖不足——启用的信息源太少，暂时无法形成可靠判断。')}</div>${EMPTY_HTML}`;
  } else {
    root.innerHTML = `<div class="u-max">${header}${candidatesBlock}</div>`;
  }
  // 内联告诉远见折叠框（复用 tell 模块）
  const slot = root.querySelector('.tell-slot');
  if (slot) {
    slot.innerHTML = tellBoxHtml('inline');
    bindTellBox(slot);
  }
  // 候选预测确认绑定：调 /api/cognition/candidates/{id}/confirm
  root.querySelectorAll('.candidate[data-id]').forEach(row => {
    const btn = row.querySelector('[data-confirm]');
    btn.addEventListener('click', () => withBusy(btn, '记录中…', async () => {
      try {
        await api(`/api/cognition/candidates/${encodeURIComponent(row.dataset.id)}/confirm`, {
          method: 'POST',
          body: JSON.stringify({probability: Number(row.querySelector('select').value) / 100})
        });
        showToast('已确认，远见会在截止日检查实际结果');
        row.remove();
      } catch (error) {
        showToast(`确认失败：${error.message}`, 'err');
      }
    }));
  });
  // data-go 跳转链：直接改 hash，避免 router ↔ views 循环依赖
  root.querySelectorAll('[data-go-view]').forEach(btn => {
    btn.addEventListener('click', () => { location.hash = `#/${btn.dataset.view}`; });
  });
}
