// 远见 v0.9 · 今日远见 —— 1 个深度预知（按 GYW 框架分析）+ 预测进度（AC-01）
import { api, escapeHtml, showPageError, showToast, withBusy } from '../api.js';
import { yjIcon } from '../icons.js';
import { riskTag, categoryLabel, formatLocalTime } from '../ui_core.js';
import { tellBoxHtml, bindTellBox } from './tell.js';

const PROBS = [90, 80, 70, 60, 50, 40, 30, 20, 10];

// 利益分析框架：基于用户的《登高望远》方法论（GYW-005/006/007/009/012）。
// 每个候选预测必须有：参与方激励、经济约束、最小阻力路径、反对证据、领先指标。
// 这五项不是替换 judgment 的事实摘要，而是在它之上加一层结构化视角，
// 帮用户看到"这件事能不能成 / 谁会推动 / 谁会反对"。
function gywAnalysis(category) {
  const cat = String(category || 'general').casefold();
  const map = {
    cashflow: {
      stakeholders: '推动方：金融机构、付款方；阻力方：风控合规、不良资产',
      constraint: '现金流约束：银行不良率 / 地方债务 / 上下游账期',
      least_resistance: '最小阻力路径：分期付款 / 展期 / 国资兜底',
      counter: '反对证据：政策叫停 / 流动性收紧 / 反腐审计',
      leading: '领先指标：实际拨付时间 / 配套政策落地',
    },
    assets: {
      stakeholders: '推动方：持有人、资产管理人；阻力方：监管、税务',
      constraint: '价值约束：估值波动 / 流动性 / 政策风险',
      least_resistance: '最小阻力路径：分批处置 / 公告延期',
      counter: '反对证据：监管调查 / 评级下调',
      leading: '领先指标：公告 / 评级变动',
    },
    work: {
      stakeholders: '推动方：雇主、地方政府；阻力方：工会、员工',
      constraint: '成本约束：企业利润空间 / 财政补贴',
      least_resistance: '最小阻力路径：分阶段执行 / 试点先行',
      counter: '反对证据：经济下行 / 财政紧张',
      leading: '领先指标：地方实施细则 / 行业响应',
    },
    policy: {
      stakeholders: '推动方：发文机关、上级政府；阻力方：执行部门、被监管方',
      constraint: '资源约束：财政预算 / 编制 / 配套立法',
      least_resistance: '最小阻力路径：试点 → 推广 → 全面执行',
      counter: '反对证据：执行阻力 / 利益集团游说 / 媒体质疑',
      leading: '领先指标：试点公告 / 配套细则 / 部门预算',
    },
    opportunity: {
      stakeholders: '推动方：投资人、地方政府、产业方；阻力方：竞争者、监管',
      constraint: '市场约束：需求 / 资本 / 关键技术',
      least_resistance: '最小阻力路径：先小规模试水 → 复制扩张',
      counter: '反对证据：竞争者抢先 / 政策转向',
      leading: '领先指标：投资公告 / 试点规模',
    },
    family: {
      stakeholders: '推动方：家庭成员；阻力方：其他家庭成员、时间',
      constraint: '资源约束：时间 / 金钱 / 精力',
      least_resistance: '最小阻力路径：分阶段执行 / 借力外部',
      counter: '反对证据：家庭沟通阻力 / 突发情况',
      leading: '领先指标：家庭讨论结果 / 资源到位',
    },
  };
  return map[cat] || map.cashflow;
}

function candidateCard(c) {
  const analysis = gywAnalysis(c.category);
  return `<div class="candidate" data-id="${escapeHtml(c.id)}">
    <div class="u-flex1">
      <p class="today-title">${escapeHtml(c.statement || c.summary || '候选预测')}</p>
      <p class="u-dim">${escapeHtml(categoryLabel(c.category))} · 截止 ${escapeHtml(formatLocalTime(c.window_end))}</p>
    </div>
    <details class="gyw-analysis">
      <summary>远见按《登高望远》框架的分析</summary>
      <ul class="body">
        <li><strong>谁会推动？谁会反对？</strong> ${escapeHtml(analysis.stakeholders)}</li>
        <li><strong>什么约束可能让它延迟？</strong> ${escapeHtml(analysis.constraint)}</li>
        <li><strong>最小阻力路径是什么？</strong> ${escapeHtml(analysis.least_resistance)}</li>
        <li><strong>反对证据 / 替代路径：</strong> ${escapeHtml(analysis.counter)}</li>
        <li><strong>领先指标（出现即要警觉）：</strong> ${escapeHtml(analysis.leading)}</li>
      </ul>
    </details>
    <div class="u-row">
      <select aria-label="你判断这件事发生的概率" title="选完后点确认，远见会在截止日检查并记入 Brier 分数。">
        ${PROBS.map(p => `<option value="${p}">${p}%</option>`).join('')}
      </select>
      <button type="button" class="btn btn-sm btn-primary" data-confirm>确认</button>
    </div>
  </div>`;
}

// 1 个深度预知 = 1 个候选预测 + 利益分析框架，不是列表。
// 避免和校准面板重复堆 5 条；用户来首页是要"看穿一件事"，不是"扫一眼"。
function deepDiveHtml(candidate) {
  if (!candidate) return '';
  return `<section class="u-max">
    <h2 class="section-title">今日一个深度预知</h2>
    ${candidateCard(candidate)}
  </section>`;
}

// 预测进度：已结算数 / 命中 / 失误 / 本周到期的预测数。
// 这才是首页"预测结果分析"该有的内容——空着也比堆待确认列表强。
function progressHtml(progress) {
  if (!progress) return '';
  const { resolved_total, hit_total, miss_total, due_this_week } = progress;
  return `<section class="card u-mb-md">
    <div class="u-row">
      <div class="kpi-block"><div class="kpi-num">${escapeHtml(String(resolved_total ?? 0))}</div><div class="kpi-label">已结算预测</div></div>
      <div class="kpi-block"><div class="kpi-num">${escapeHtml(String(hit_total ?? 0))}</div><div class="kpi-label">历史命中</div></div>
      <div class="kpi-block"><div class="kpi-num">${escapeHtml(String(miss_total ?? 0))}</div><div class="kpi-label">历史失误</div></div>
      <div class="kpi-block"><div class="kpi-num">${escapeHtml(String(due_this_week ?? 0))}</div><div class="kpi-label">本周到期</div></div>
    </div>
    <p class="u-dim u-mt-sm">预测健康度——确认预测 + 等到期结算，远见会自动统计你的判断准不准。</p>
  </section>`;
}

export async function render(root) {
  let progress = null;
  let candidate = null;
  try {
    progress = await api('/api/forecasts/progress');
  } catch (_) { progress = null; }
  try {
    const response = await api('/api/cognition/candidates?limit=1');
    const list = Array.isArray(response?.candidates) ? response.candidates
      : Array.isArray(response) ? response : [];
    candidate = list[0] || null;
  } catch (_) { candidate = null; }

  root.innerHTML = `<div class="u-max">
    ${progressHtml(progress)}
    ${deepDiveHtml(candidate)}
  </div>`;

  // 内联告诉远见折叠框（复用 tell 模块）
  const tellSlot = document.createElement('div');
  tellSlot.className = 'u-max u-mt-md';
  tellSlot.innerHTML = `<details class="tell card"><summary class="section-title">告诉远见 · 有新情况要记录</summary><div class="tell-slot u-mt-sm"></div></details>`;
  root.appendChild(tellSlot);
  const inlineSlot = tellSlot.querySelector('.tell-slot');
  if (inlineSlot) {
    inlineSlot.innerHTML = tellBoxHtml('inline');
    bindTellBox(inlineSlot);
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
