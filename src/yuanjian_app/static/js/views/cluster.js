// 远见 v1.5.3 · 事件详情页
//
// 这一页是**方法论产物唯一的出口**。v1.3 接上了此前"算了但没人看得到"的东西：
//   · beneficiaries / cost_bearers（谁获利、谁承担成本）—— 用户的核心方法论
//   · scenario_paths（多路径推演）—— 此前字段名两边不一致，面板永远渲染不出来
//   · power_structure（权力结构判定 + 判读依据）
//   · risk_signal_hit（「慷慨激昂」命中的词）
//   · historical_parallel_detail（历史押韵，并标明是模板类比而非检索结果）
//   · magnitude（量级：没有数字就写"未量化"）
//
// 同时修掉一个 v1.2 引入的缺陷：renderGywSection(gyw) 内部引用了不在其作用域的
// 变量 `j`（分析状态提示），会抛 ReferenceError 并让整页显示"加载失败"。
// 现在状态与数据都显式传参。
import { api, escapeHtml, showLoading, showPageError } from '../api.js';

// 关注度档位（**不是**风险量级）——见 ui_core.js 里 riskLabel 的口径说明
const ATTENTION_LABELS = {
  L4: 'L4 · 重点关注',
  L3: 'L3 · 需要关注',
  L2: 'L2 · 低优先',
  L1: 'L1 · 仅记录',
};

function alertBadge(level) {
  return `<span class="badge badge-alert-${level || 'L2'}">${ATTENTION_LABELS[level] || level || ''}</span>`;
}

function evidenceBadge(level) {
  const labels = { E3: 'E3 · 多源互证', E2: 'E2 · 双源', E1: 'E1 · 单源待证' };
  return `<span class="badge badge-evidence">${labels[level] || level || ''}</span>`;
}

// 分析状态提示：非 real 时必须显式说出来（v1.2 第三波）
function analysisNotice(status) {
  if (status !== 'degraded' && status !== 'placeholder') return '';
  const why = status === 'placeholder' ? '兜底占位' : '字段修补';
  return `<p class="u-dim">本次未生成有效分析（来源：${escapeHtml(why)}）` +
    `——以下内容仅供占位，不要据此做判断。</p>`;
}

// 结构化条目（beneficiaries / cost_bearers）渲染：{subject, gain|cost, evidence_refs}
function renderStructuredEntries(items, modeKey, emptyText) {
  if (!Array.isArray(items) || !items.length) {
    return `<p class="u-dim">${escapeHtml(emptyText)}</p>`;
  }
  return `<ul class="gyw-list">${items.map(item => {
    const subject = escapeHtml(String(item.subject || ''));
    const detail = escapeHtml(String(item[modeKey] || ''));
    const refs = Array.isArray(item.evidence_refs) ? item.evidence_refs.filter(Boolean) : [];
    // [推断] 前缀 = 无来源支撑；有 evidence_refs 才是从证据里读出来的
    const tag = subject.startsWith('[推断]') || !refs.length
      ? '<span class="tag tag-low">推断</span>'
      : `<span class="tag tag-high">有出处</span>`;
    const refLine = refs.length
      ? `<span class="u-dim">出处：${refs.map(r => escapeHtml(String(r))).join('、')}</span>`
      : '<span class="u-dim">无来源支撑</span>';
    return `<li>${tag} <strong>${subject}</strong>：${detail} ${refLine}</li>`;
  }).join('')}</ul>`;
}

function renderPowerStructure(power) {
  if (!power || !power.rule) return '';
  const orgs = Array.isArray(power.matched_orgs) ? power.matched_orgs.filter(Boolean) : [];
  const delay = String(power.delay_risk || '未知');
  const delayClass = delay === '高' ? 'text-warn' : 'u-dim';
  return `<div class="gyw-item gyw-wide">
    <h4>① 权力结构判定</h4>
    <p>
      <strong>发文层级：</strong>${escapeHtml(String(power.rule))}　
      <strong>执行层：</strong>${escapeHtml(String(power.execution_layer || ''))}　
      <strong class="${delayClass}">执行阻力：${escapeHtml(delay)}</strong>
    </p>
    <p>${escapeHtml(String(power.veto_analysis || ''))}</p>
    ${orgs.length
      ? `<p class="u-dim">识别到的机构：${orgs.map(o => escapeHtml(String(o))).join('、')}</p>`
      : '<p class="u-dim">证据中未识别出机构名——权力结构判为「未知」，不猜。</p>'}
    ${power.basis ? `<p class="u-dim">判读依据：${escapeHtml(String(power.basis))}</p>` : ''}
  </div>`;
}

function renderRiskSignal(hits) {
  const keywords = Array.isArray(hits) ? hits.filter(Boolean) : [];
  if (!keywords.length) return '';
  return `<div class="gyw-item gyw-wide">
    <h4>⑧ 风险信号（慷慨激昂）</h4>
    <p class="text-warn">命中：${keywords.map(k => escapeHtml(String(k))).join('、')}</p>
    <p class="u-dim">方法论：「慷慨激昂往往是内心已感知风险的表达」。命中即**上调关注度等级**，
      不是"我们判断得更准"。（v1.3 修正：旧代码在这里上调的是置信度，方向反了。）</p>
  </div>`;
}

function renderLeadingBoost(hits, boost) {
  const items = Array.isArray(hits) ? hits.filter(Boolean) : [];
  if (!items.length) return '';
  const total = Number(boost || 0);
  return `<p class="u-dim">领先信号已计入概率中心：合计权重 +${(total * 100).toFixed(0)}%` +
    `（${items.map(m => escapeHtml(String(m.signal || m.pattern || ''))).join('；')}）</p>`;
}

function renderHistorical(detail, plainText) {
  if (detail && detail.event) {
    const keywords = Array.isArray(detail.matched_keywords) ? detail.matched_keywords : [];
    return `<div class="gyw-item gyw-wide">
      <h4>⑦ 历史押韵</h4>
      <p><strong>${escapeHtml(String(detail.event))}</strong>
        <span class="tag tag-low">${escapeHtml(String(detail.source || '本机模板类比'))}</span></p>
      <p>相似点：${escapeHtml(String(detail.similarity || ''))}</p>
      <p>不同点：${escapeHtml(String(detail.difference || ''))}</p>
      ${keywords.length
        ? `<p class="u-dim">匹配关键词：${keywords.map(k => escapeHtml(String(k))).join('、')}</p>`
        : ''}
      <p class="u-dim">这是本机模板里的类比，**不是检索到的史料**，用之前请自行核验。</p>
    </div>`;
  }
  if (plainText) {
    return `<div class="gyw-item gyw-wide">
      <h4>⑦ 历史押韵</h4>
      <p>${escapeHtml(String(plainText))}</p>
    </div>`;
  }
  return `<div class="gyw-item gyw-wide">
    <h4>⑦ 历史押韵</h4>
    <p class="u-dim">无可比历史——本机模板在该领域没有可比事件，不硬套。</p>
  </div>`;
}

function renderGywSection(gyw, status) {
  if (!gyw || !gyw.stakeholders) return '';
  return `<section class="card u-mb-md">
    <h3 class="section-title">登高望远 · 六步分析</h3>
    ${analysisNotice(status)}
    <div class="gyw-grid">
      <div class="gyw-item">
        <h4>① 权力结构</h4>
        <p>${escapeHtml(gyw.stakeholders || '-')}</p>
      </div>
      <div class="gyw-item">
        <h4>② 利益方向 · 谁获利</h4>
        ${renderStructuredEntries(gyw.beneficiaries, 'gain', '未给出获利方（证据不足或未被分析）')}
      </div>
      <div class="gyw-item">
        <h4>② 利益方向 · 谁承担成本</h4>
        ${renderStructuredEntries(gyw.cost_bearers, 'cost', '未给出承担成本方（证据不足或未被分析）')}
      </div>
      ${renderPowerStructure(gyw.power_structure)}
      <div class="gyw-item">
        <h4>③ 结构约束</h4>
        <p>${escapeHtml(gyw.constraints || '-')}</p>
      </div>
      <div class="gyw-item">
        <h4>④ 最小阻力路径</h4>
        <p>${escapeHtml(gyw.least_resistance_path || '-')}</p>
      </div>
      <div class="gyw-item">
        <h4>⑤ 反面证据</h4>
        <p class="text-warn">${escapeHtml(gyw.counter_evidence || '-')}</p>
      </div>
      <div class="gyw-item">
        <h4>⑥ 领先指标</h4>
        <p>${escapeHtml(gyw.leading_indicators || '-')}</p>
        ${renderLeadingBoost(gyw.leading_indicator_hits, gyw.leading_boost)}
      </div>
      ${renderHistorical(gyw.historical_parallel_detail, gyw.historical_parallel)}
      ${renderRiskSignal(gyw.risk_signal_hit)}
    </div>
  </section>`;
}

function renderScenarios(scenarios) {
  if (!Array.isArray(scenarios) || !scenarios.length) return '';
  return `<section class="card u-mb-md">
    <h3 class="section-title">多路径推演</h3>
    <div class="scenario-list">
      ${scenarios.map(s => {
        const label = escapeHtml(String(s.label || s.path_type || '路径'));
        const cls = s.path_type === 'black_swan' ? 'scenario-swan'
          : (s.path_type === 'secondary' ? 'scenario-secondary' : 'scenario-most');
        // probability 恒为 null：本机模板无权给路径赋概率。
        // 黑天鹅尤其不能给数字——那等于声称视线覆盖了视野之外。
        const probLine = (typeof s.probability === 'number')
          ? `<span class="scenario-prob">概率 ${Math.round(s.probability * 100)}%</span>`
          : `<span class="scenario-prob u-dim">不给概率</span>`;
        return `<div class="scenario-item ${cls}">
          <div class="scenario-header"><strong>${label}</strong>${probLine}</div>
          <p class="scenario-desc">${escapeHtml(String(s.description || ''))}</p>
          ${s.trigger ? `<div class="scenario-watch"><strong>观察信号：</strong>${escapeHtml(String(s.trigger))}</div>` : ''}
          ${s.probability_note ? `<div class="u-dim">${escapeHtml(String(s.probability_note))}</div>` : ''}
        </div>`;
      }).join('')}
    </div>
  </section>`;
}

function renderImpacts(impacts, clusterId) {
  if (!Array.isArray(impacts) || !impacts.length) return '';
  return `<section class="card u-mb-md">
    <h3 class="section-title">对你的个人影响</h3>
    <div class="impact-list">
      ${impacts.map(imp => {
        const c = imp.candidate || {};
        const confirmed = !!c.confirmed_forecast_id;
        // 基准率只采**人工确认**的二元结算（R-04），所以样本构成必须写出来：
        // 只说"样本不足"，用户既不知道差在哪，也不知道要怎么做才能补上。
        const composition = c.base_rate_composition || {};
        const userSample = Number(c.base_rate_sample || composition.user || 0);
        const totalSample = Number(c.base_rate_sample_total || composition.total || userSample);
        const sampleNote = `本类别样本 ${totalSample} 条（其中人工 ${userSample} 条）`;
        const baseRate = (c.base_rate === null || c.base_rate === undefined)
          ? `基准率：样本不足——${sampleNote}；人工样本不足 5 条就不估`
          : `基准率：${Math.round(Number(c.base_rate) * 100)}%——${sampleNote}`;
        // 证伪性闸的结论（R-08/R-09）：候选停在"待补充"时，把原因写在卡上，
        // 而不是等用户点了"记录预测"才弹一个错——那会被当成 bug。
        const gateNote = c.main_signal_preexisting
          ? '主信号在窗口开始前已成立（零证伪风险），要换一条落在窗口内的信号才能入账。'
          : (!c.observable_signals
            ? '可观测信号未特化，需人工补充：至少一条要含本事件的机构名/地名/数字。'
            : '');
        const riskHits = Array.isArray(c.risk_signal_hit) ? c.risk_signal_hit.filter(Boolean) : [];
        return `<div class="impact-row" data-impact="${imp.impact_id}">
          <div class="impact-main">
            <div class="impact-header">
              ${alertBadge(imp.alert_level)}
              <span class="impact-name">${escapeHtml(imp.interest_name || '已登记利益')}</span>
              ${confirmed ? '<span class="badge badge-confirmed">✓ 已记录预测</span>' : '<span class="badge badge-pending">待确认</span>'}
            </div>
            <div class="impact-title">${escapeHtml(c.title || '')}</div>
            ${c.recommended_action ? `<div class="impact-action">▸ ${escapeHtml(c.recommended_action)}</div>` : ''}
            <div class="impact-meta">
              ${c.window_end ? `<span>窗口期截止：${escapeHtml(c.window_end)}</span>` : ''}
              ${!confirmed ? `<span>系统估计概率：${Math.round((c.probability_low||0)*100)}% — ${Math.round((c.probability_high||0)*100)}%</span>` : ''}
            </div>
            <div class="impact-meta u-dim">
              <span>${escapeHtml(baseRate)}</span>
            </div>
            ${c.magnitude_line
              ? `<div class="impact-meta"><span>${escapeHtml(String(c.magnitude_line))}</span></div>`
              : '<div class="impact-meta u-dim"><span>量级 未量化（证据中无金额/数量）</span></div>'}
            ${riskHits.length
              ? `<div class="impact-meta u-dim"><span>风险上调依据：命中「${riskHits.map(k => escapeHtml(String(k))).join('、')}」</span></div>`
              : ''}
            ${c.observable_signals
              ? `<div class="impact-meta u-dim"><span>结算看点：${escapeHtml(String(c.observable_signals))}</span></div>`
              : ''}
            ${gateNote && !confirmed
              ? `<div class="impact-meta text-warn"><span>${escapeHtml(gateNote)}</span></div>`
              : ''}
          </div>
          ${!confirmed ? `<button class="btn-primary btn-sm" data-action="confirm-from-detail" data-impact="${imp.impact_id}" data-cluster="${clusterId}">记录预测</button>` : ''}
        </div>`;
      }).join('')}
    </div>
  </section>`;
}

function renderSources(items, domains, cluster) {
  if (!Array.isArray(items) || !items.length) return '';
  const domainLine = Array.isArray(domains) && domains.length
    ? `<div class="u-dim">来源域名：${domains.map(d => escapeHtml(String(d))).join('、')}</div>`
    : '';
  // 同文转载（R-10）：条目身份此前只按 canonical_url 去重，于是同一篇通稿被 5 家
  // 门户转载 = 5 个独立域名 = 直接进 E2。界面只说"来源多"而不说"其中多少是同一篇
  // 通稿"，等于把复制当成了互证。这里照实写出来。
  //
  // v1.4 之前写入的事件簇没有这两列（迁移不回填），两个数可能对不上 —— 后端会把
  // 它们一起标成 null。**必须用 typeof 判数字**：写成 `Number(x || 0) > 0` 会把
  // "未知"读成 0 而恰好不显示（结果对、理由错），一旦有人把判断改成 `>= 0`
  // 就会印出「0 个为同文转载」这种假事实。
  const total = cluster?.source_domains;
  const syndicated = cluster?.syndicated_domains;
  const syndicationLine = (
    typeof total === 'number' && typeof syndicated === 'number' && syndicated > 0
  )
    ? `<div class="text-warn">本事件 ${total} 个来源中 ${syndicated} 个为同文转载——已按 1 个独立声音计入证据等级。</div>`
    : '';
  return `<section class="card u-mb-md">
    <h3 class="section-title">证据来源（${items.length} 条）</h3>
    ${domainLine}
    ${syndicationLine}
    <div class="source-evidence-list">
      ${items.slice(0, 20).map(item => `
        <div class="evidence-item">
          <div class="evidence-title">
            <a href="${escapeHtml(item.canonical_url || '#')}" target="_blank" rel="noopener">${escapeHtml(item.title || '(无标题)')}</a>
          </div>
          <div class="evidence-meta">
            <span>${escapeHtml(item.source_domain || '')}</span>
            ${item.published_at ? `<span>${escapeHtml(String(item.published_at).slice(0,10))}</span>` : ''}
          </div>
          ${item.summary ? `<div class="evidence-summary">${escapeHtml(item.summary.slice(0, 300))}</div>` : ''}
        </div>
      `).join('')}
      ${items.length > 20 ? `<div class="evidence-more">还有 ${items.length - 20} 条来源未显示</div>` : ''}
    </div>
  </section>`;
}

function renderFactChain(judgment, sourceDomains) {
  if (!judgment) return '';
  const actors = judgment.actors || [];
  const chain = judgment.causal_chain || [];
  const up = judgment.up_triggers || [];
  const down = judgment.down_triggers || [];
  const domains = (Array.isArray(sourceDomains) && sourceDomains.length)
    ? sourceDomains
    : (judgment.gyw && judgment.gyw.source_domains) || [];
  // ⚠ actors 的语义在 v1.3 被修正：它此前装的是**来源域名**，
  // 界面把"某网站"当成了"当事方"。现在 actors = 参与方（机构/群体），
  // 域名单独列在下面。
  return `<section class="card u-mb-md">
    <h3 class="section-title">事实摘要与因果链</h3>
    <div class="fact-summary">${escapeHtml(judgment.fact_summary || '暂无摘要')}</div>
    ${actors.length ? `<div class="fact-actors"><strong>相关方：</strong>${actors.map(a => `<span class="actor-tag">${escapeHtml(typeof a === 'string' ? a : a.name || JSON.stringify(a))}</span>`).join('')}</div>` : ''}
    ${domains.length ? `<div class="fact-actors u-dim"><strong>来源域名：</strong>${domains.map(d => `<span class="actor-tag">${escapeHtml(String(d))}</span>`).join('')}</div>` : ''}
    ${chain.length ? `<div class="fact-chain">
      <h4>因果链条：</h4>
      <ol>${chain.map(step => `<li>${escapeHtml(typeof step === 'string' ? step : step.description || JSON.stringify(step))}</li>`).join('')}</ol>
    </div>` : ''}
    ${up.length || down.length ? `<div class="triggers">
      ${up.length ? `<div class="trigger-up"><strong>↑ 概率上调信号：</strong>${escapeHtml(up.join('；'))}</div>` : ''}
      ${down.length ? `<div class="trigger-down"><strong>↓ 概率下调信号：</strong>${escapeHtml(down.join('；'))}</div>` : ''}
    </div>` : ''}
  </section>`;
}

export async function render(root) {
  // 从hash解析cluster_id: #/cluster/XXX
  const hash = location.hash || '';
  const clusterId = hash.replace(/^#\/cluster\//, '').split('?')[0];
  if (!clusterId) {
    showPageError(root, '事件ID无效', () => location.hash = '#/today');
    return;
  }

  showLoading(root, '正在加载事件详情…');

  try {
    const data = await api(`/api/cognition/clusters/${clusterId}`);
    const j = data.judgment || {};
    const gyw = j.gyw || {};
    const scenarios = gyw.scenario_paths || j.scenario_paths || [];
    const confidence = j.confidence != null ? `${Math.round(Number(j.confidence) * 100)}%` : '待评估';
    const horizon = Array.isArray(j.horizons) ? j.horizons.join('，') : (j.horizons || '');
    const magnitudeLine = (data.impacts && data.impacts[0] && data.impacts[0].candidate
      && data.impacts[0].candidate.magnitude_line) || '量级 未量化';

    root.innerHTML = `<div class="u-max">
      <div class="page-nav">
        <a href="#/today" class="btn-text">← 返回行动雷达</a>
      </div>
      <section class="card u-mb-md">
        <div class="detail-header">
          <div class="detail-badges">
            ${alertBadge(data.impacts?.[0]?.alert_level || 'L3')}
            ${evidenceBadge(data.evidence_level)}
            <span class="badge badge-conf">置信度 ${escapeHtml(confidence)}</span>
            ${horizon ? `<span class="badge badge-window">窗口：${escapeHtml(horizon)}</span>` : ''}
            <span class="badge badge-window">${escapeHtml(String(magnitudeLine))}</span>
          </div>
          <h2 class="detail-title">${escapeHtml(data.title || '(无标题事件)')}</h2>
          <p class="detail-summary">${escapeHtml(data.summary || '')}</p>
          <div class="detail-meta">
            <span>首次发现：${escapeHtml(String(data.first_seen_at || '').slice(0, 16).replace('T', ' '))}</span>
            <span>最近更新：${escapeHtml(String(data.last_seen_at || '').slice(0, 16).replace('T', ' '))}</span>
            ${j.provider ? `<span>研判来源：${j.provider === 'local' ? '本地启发式' : escapeHtml(j.provider)}</span>` : ''}
          </div>
          <p class="u-dim">档位说明：「重点关注 / 需要关注」衡量的是**这件事与你的相关度与紧迫度**，
            不是损失量级。要看量级，看上方那一行——证据里没有数字时它写「未量化」。</p>
        </div>
      </section>
      ${renderFactChain(j, gyw.source_domains)}
      ${renderGywSection(gyw, j.analysis_status)}
      ${renderScenarios(scenarios)}
      ${renderImpacts(data.impacts, clusterId)}
      ${renderSources(data.items, gyw.source_domains, data)}
      <div class="page-nav u-mt-md">
        <a href="#/today" class="btn-text">← 返回行动雷达</a>
      </div>
    </div>`;

    // 绑定详情页的确认预测按钮
    bindDetailActions(root, clusterId);
  } catch (err) {
    showPageError(root, `加载失败：${err.message || '未知错误'}`, () => render(root));
  }
}

function bindDetailActions(root, clusterId) {
  // N1 同款修复（与 today.js 同一套写法）：#view-root 是持久节点，cluster 视图每次
  // render / 导航都会再调用本函数。旧实现每次都往同一节点挂新的 click 监听 → 监听随
  // 进出次数线性叠加，一次点击会重复触发。改成「先按引用移除旧监听再绑定」，保证
  // 进出 N 次后单次点击仍只触发一次。
  if (root._clusterDetailClick) {
    root.removeEventListener('click', root._clusterDetailClick);
  }

  const handler = async (e) => {
    const btn = e.target.closest('button[data-action="confirm-from-detail"]');
    if (!btn) return;
    const impactId = btn.dataset.impact;
    // 简化：跳转到校准面板进行确认
    location.hash = '#/calib';
  };

  root._clusterDetailClick = handler;
  root.addEventListener('click', handler);
}
