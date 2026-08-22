// 远见 v0.9 · 今日远见 —— 1 个深度预知（按 GYW 框架分析）+ 预测进度（AC-01）
import { api, escapeHtml, showPageError, showToast, withBusy } from '../api.js';
import { yjIcon } from '../icons.js';
import { riskTag, categoryLabel, formatLocalTime, sourceBadge } from '../ui_core.js';
import { tellBoxHtml, bindTellBox } from './tell.js';

const PROBS = [95, 90, 80, 65, 50, 35, 20, 10, 5];

// GYW 框架的 UI 兜底模板：当后端没给 gyw 子结构（旧 judgment / 远端未填）
// 才用这套 category 模板。新版 LocalHeuristicProvider 已直接生成 gyw 字段，
// 优先消费 candidate.gyw 而不是这套兜底。
function gywFallback(category) {
  const cat = String(category || 'general').toLowerCase();
  const map = {
    cashflow: {
      stakeholders: '推动方：付款方、金融机构；阻力方：风控合规、审计',
      constraints: '现金流约束：银行不良率、上下游账期、企业利润空间',
      least_resistance_path: '最小阻力路径：分期拨付 / 展期重组 / 国资兜底',
      counter_evidence: '反对证据：政策叫停、流动性收紧、反腐审计',
      leading_indicators: '领先指标：实际拨付时间、配套政策落地',
    },
    finance: {
      stakeholders: '推动方：监管、机构投资者；阻力方：散户、合规',
      constraints: '市场约束：流动性、估值、跨境资本',
      least_resistance_path: '最小阻力路径：渐进调整 / 试点先行',
      counter_evidence: '反对证据：监管反向、市场恐慌、外部冲击',
      leading_indicators: '领先指标：监管口径、北向资金、信用利差',
    },
    policy: {
      stakeholders: '推动方：发文机关、上级政府；阻力方：执行部门、利益集团',
      constraints: '资源约束：财政预算、编制、配套立法',
      least_resistance_path: '最小阻力路径：试点 → 推广 → 全面执行',
      counter_evidence: '反对证据：执行阻力、利益集团游说、政策转向',
      leading_indicators: '领先指标：试点公告、配套细则、部门预算',
    },
    work: {
      stakeholders: '推动方：雇主、地方政府；阻力方：工会、员工',
      constraints: '成本约束：企业利润空间、财政补贴',
      least_resistance_path: '最小阻力路径：分阶段执行 / 试点先行',
      counter_evidence: '反对证据：经济下行、财政紧张、企业抵制',
      leading_indicators: '领先指标：地方实施细则、行业响应',
    },
    opportunity: {
      stakeholders: '推动方：投资人、地方政府、产业方；阻力方：竞争者、监管',
      constraints: '市场约束：需求、资本、关键技术',
      least_resistance_path: '最小阻力路径：先小规模试水 → 复制扩张',
      counter_evidence: '反对证据：竞争者抢先、政策转向、技术失败',
      leading_indicators: '领先指标：投资公告、试点规模、关键客户签约',
    },
    family: {
      stakeholders: '推动方：家庭成员；阻力方：其他家庭成员、时间',
      constraints: '资源约束：时间、金钱、精力',
      least_resistance_path: '最小阻力路径：分阶段执行 / 借力外部',
      counter_evidence: '反对证据：家庭沟通阻力、突发情况',
      leading_indicators: '领先指标：家庭讨论结果、资源到位',
    },
  };
  return map[cat] || {
    stakeholders: '推动方：事件发起方；阻力方：执行部门、外部不确定',
    constraints: '资源约束：财政、编制、执行能力、外部配合',
    least_resistance_path: '最小阻力路径：分阶段执行 / 试点先行',
    counter_evidence: '反对证据：执行阻力、政策转向、外部冲击',
    leading_indicators: '领先指标：配套细则、试点公告、执行进度',
  };
}

// 解析 candidate 的 GYW 分析：优先用后端 judgment.gyw（真实结构化字段），
// 没有时回退到 UI 兜底模板。来源标注统一走 ui_core.sourceBadge（稿C v2），
// 界面词由 judgments.provider 驱动，消灭"贴标签"。
function resolveGyw(candidate) {
  const backend = candidate?.gyw;
  const hasComplete = backend && typeof backend === 'object'
      && backend.stakeholders && backend.constraints
      && backend.least_resistance_path && backend.counter_evidence
      && backend.leading_indicators;
  if (hasComplete) {
    const { text } = sourceBadge(candidate);
    return { analysis: backend, source: text };
  }
  // Truly nothing on the backend — show UI fallback, but only for new
  // candidates that the engine never produced gyw for. Today this path
  // is unreachable in practice (pending_candidates always backfills), but
  // keep it as a defensive last-resort. 来源标注统一走 sourceBadge 占位分支。
  const { text } = sourceBadge(candidate);
  return { analysis: gywFallback(candidate?.category), source: text };
}

function candidateCard(c) {
  const { analysis, source } = resolveGyw(c);
  const factSummary = c?.fact_summary || c?.statement || c?.summary || '候选预测';
  return `<div class="candidate" data-id="${escapeHtml(c.id)}">
    <div class="u-flex1">
      <p class="today-title">${escapeHtml(c.statement || c.summary || '候选预测')}</p>
      <p class="u-dim">${escapeHtml(categoryLabel(c.category))} · 截止 ${escapeHtml(formatLocalTime(c.window_end))}</p>
      ${c?.actors?.length ? `<p class="u-dim">参与方：${escapeHtml(c.actors.join('、'))}</p>` : ''}
    </div>
    <details class="gyw-analysis">
      <summary>远见按《登高望远》框架的分析（来源：${source}）</summary>
      <ul class="body">
        <li><strong>事实摘要：</strong> ${escapeHtml(factSummary)}</li>
        <li><strong>谁会推动？谁会反对？</strong> ${escapeHtml(analysis.stakeholders)}</li>
        <li><strong>什么约束可能让它延迟？</strong> ${escapeHtml(analysis.constraints)}</li>
        <li><strong>最小阻力路径是什么？</strong> ${escapeHtml(analysis.least_resistance_path)}</li>
        <li><strong>反对证据 / 替代路径：</strong> ${escapeHtml(analysis.counter_evidence)}</li>
        <li><strong>领先指标（出现即要警觉）：</strong> ${escapeHtml(analysis.leading_indicators)}</li>
      </ul>
    </details>
    <div class="u-row">
      ${c.confirmed ?
        `<span class="u-dim">已自动确认 · 系统判断概率 ${Math.round((c.confirmed_probability || 0) * 100)}%</span>` :
        `<select aria-label="你判断这件事发生的概率" title="选完后点确认，远见会在截止日检查并记入 Brier 分数。">
          ${PROBS.map(p => `<option value="${p}">${p}%</option>`).join('')}
        </select>
        <button type="button" class="btn btn-sm btn-primary" data-confirm>确认</button>`
      }
    </div>
    ${c?.cluster_id ? `<div class="u-row u-feedback u-mt-sm">
      <button type="button" class="btn btn-sm" data-feedback="false_positive" data-cluster="${escapeHtml(c.cluster_id)}">误报</button>
      <button type="button" class="btn btn-sm" data-feedback="mute" data-cluster="${escapeHtml(c.cluster_id)}">静音</button>
      <button type="button" class="btn btn-sm" data-feedback="lower_importance" data-cluster="${escapeHtml(c.cluster_id)}">降低</button>
    </div>` : ''}
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
    if (!btn) return; // 已自动确认的候选没有确认按钮
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
  // 误报反馈：误报 / 静音 / 降低重要度 → POST /api/cognition/clusters/{id}/feedback
  root.querySelectorAll('[data-feedback]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const clusterId = btn.dataset.cluster;
      const action = btn.dataset.feedback;
      if (!clusterId) { showToast('该候选没有可反馈的事件', 'err'); return; }
      btn.disabled = true;
      try {
        await api(`/api/cognition/clusters/${encodeURIComponent(clusterId)}/feedback`, {
          method: 'POST',
          body: JSON.stringify({action})
        });
        showToast(action === 'false_positive' ? '已标记为误报' : action === 'mute' ? '已静音该事件' : '已降低重要度');
      } catch (e) { btn.disabled = false; showToast(`反馈失败：${e.message}`, 'err'); }
    });
  });

  // data-go 跳转链：直接改 hash，避免 router ↔ views 循环依赖
  root.querySelectorAll('[data-go-view]').forEach(btn => {
    btn.addEventListener('click', () => { location.hash = `#/${btn.dataset.view}`; });
  });
}
