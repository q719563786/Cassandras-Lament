// 校准面板的使用说明：让非技术用户能读懂每个数字的含义。
// 折叠面板默认收起，标题直接告诉用户"为什么要做这件事"。
const HELP_HTML = `<details class="card calib-help u-mb-md">
    <summary class="section-title">如何使用这个面板？</summary>
    <div class="u-mt-sm body">
      <p>这个面板衡量<strong>你的判断</strong>准不准——不只是远见猜得准不准，而是远见帮你做出的判断、加上你自己的概率选择，最后有没有真的发生。</p>
      <ul>
        <li><strong>命中率</strong>：<strong>你亲手选过概率</strong>的那些预测里，真的发生了的比例（≥50% 的才算“你说过会发生”）。100% = 全部命中。系统自动确认的条目<strong>不计入</strong>这个数，它们在下面单独列出。</li>
        <li><strong>误报率</strong>：同样是<strong>你亲手选过概率</strong>的预测里，事实没发生的比例。越低越好。</li>
        <li><strong>Brier 分数</strong>：衡量<strong>你的概率</strong>给得准不准。0 = 完美，0.25 = 一般，越低越好。计算方法：把每次预测的“你给的概率 − 实际结果”平方后求平均。机器自动填的概率不算在内——否则那是在给系统自己打分。</li>
        <li><strong>已结算预测</strong>：观察期已结束、结果已记录的预测数量。这个数越多，上面三个数字越有参考价值。</li>
        <li><strong>候选预测的百分比</strong>：这是<strong>你判断它会发生的主观概率</strong>。从九个固定档位里选一个（5% / 10% / 20% / 35% / 50% / 65% / 80% / 90% / 95%），点“确认”后，远见会在截止日期检查它是否真的发生，并把<strong>你选的</strong>概率和实际结果对比，记入 Brier 分数。<strong>只有你亲手确认的这一条，才算“你的”预测</strong>——系统自己确认的不会混进上面三个指标。</li>
      </ul>
      <p class="u-dim">一句话：选个概率 → 点确认 → 等到期看远见标对错。这是你训练自己判断力的方式。</p>
    </div>
  </details>`;

// 远见 v1.4.2 · 校准面板 —— KPI×4 / Brier 周序列 SVG / 按类别条形 / 候选确认 + 预测账本（AC-02/AC-08）
import { api, escapeHtml, showToast, paginationHtml, bindPagination, pageRange } from '../api.js';
import { statusLabel, categoryLabel, formatLocalTime } from '../ui_core.js';

// 无数据 / 端点未就绪：明示"样本不足"，绝不渲染 0 或假数（AC-02）。
//
// v1.4（R-03）：旧文案写的是「已结算的预测还太少，**等远见多跑几轮**再看校准」——
// 那句话**把机制问题说成了时间问题**。实测：v1.3 之前的 8,607 条命题的
// observable_signals 全为空，而校准准入（`_calibratable`）把它当硬门槛 →
// 它们即使补结算也**永远进不了校准**。所以文案改成如实并列两种可能，
// 不再暗示"等就会好"。
const NO_SAMPLE = `<div class="empty"><p>校准还出不了数。两种可能：<br>
① 还没有结算过任何预测——到期的预测需要你逐条或批量结算；<br>
② 已结算的条目里没有一条满足校准准入：命题缺少可观测事实，或该类别还没有足够的人工基准率样本。<br>
<b>注意：如果是第 ② 种，再等也不会自动变好。</b></p></div>`;

// 排除构成（R-03）：不能只给一个 excluded_total。用户必须看得见"为什么没进校准"，
// 而且要能分辨哪些是"再等等就好"、哪些是"永远进不了"。
function exclusionCard(calib) {
  const total = Number(calib?.excluded_total) || 0;
  const breakdown = calib?.excluded_breakdown || {};
  const lifetimeLegacy = Number(calib?.legacy_proposition_total) || 0;
  if (!total && !lifetimeLegacy) return '';
  const rows = [
    ['命题缺少可观测事实（v1.3 之前的历史批次，永远进不了）', Number(breakdown.legacy_proposition) || 0],
    ['命题可结算，但该类别人工样本不足 5 条', Number(breakdown.missing_base_rate) || 0],
    ['命题含不可结算表述（虚词等）', Number(breakdown.unsettleable) || 0],
  ];
  return `<section class="card u-mt-sm">
    <div class="u-dim">已结算但未进入校准的 ${total} 条，构成如下：</div>
    <ul>${rows.map(([label, n]) => `<li>${escapeHtml(label)}：${n} 条</li>`).join('')}</ul>
    ${lifetimeLegacy ? `<p class="text-warn">账本里另有 ${lifetimeLegacy} 条命题缺少可观测事实，不参与校准——<b>补结算不会让它们进入校准，补写可观测事实才算数。</b></p>` : ''}
  </section>`;
}

// 来源分列（R-04）：主指标只算"你亲手选过概率的"，机器与历史不明的**分列**给出、
// 不合并。合并的意义是"好看"，分列的意义是"这个分数到底在量谁"。
const SOURCE_LABELS = {user: '你选的', auto: '系统自动', unknown: '早期不明'};

function sourceBreakdownCard(calib) {
  const bySource = calib?.by_source || {};
  const rows = ['user', 'auto', 'unknown']
    .map(key => ({key, data: bySource[key] || {}}))
    .filter(item => Number(item.data.resolved_total || 0) > 0);
  if (rows.length < 2) return '';
  return `<h2 class="section-title u-mt-md">按来源分列</h2>
  <section class="card">
    <div class="u-dim">只有「你选的」计入上面的命中率 / 误报率 / Brier。机器自动确认的不并入——否则那是在给系统自己打分。</div>
    <div class="table-wrap"><table>
      <thead><tr><th>来源</th><th>可校准</th><th>命中</th><th>失误</th><th>命中率</th><th>Brier</th></tr></thead>
      <tbody>${rows.map(({key, data}) => {
        const rate = Number(data.hit_rate);
        const brier = Number(data.brier);
        return `<tr>
          <td>${escapeHtml(SOURCE_LABELS[key] || key)}</td>
          <td class="num">${Number(data.resolved_total || 0)}</td>
          <td class="num">${Number(data.hit_total || 0)}</td>
          <td class="num">${Number(data.miss_total || 0)}</td>
          <td class="num">${Number.isFinite(rate) ? `${(rate * 100).toFixed(1)}%` : '—'}</td>
          <td class="num">${Number.isFinite(brier) ? brier.toFixed(3) : '—'}</td>
        </tr>`;
      }).join('')}</tbody>
    </table></div>
  </section>`;
}

// 批量结算（R-01）：让"预测 → 结算 → 校准 → 修正"这条闭环第一次真正跑起来。
//
// 生成速度是每天几百条，而逐条结算是"每行三次交互"——开环不是使用者的疏忽，
// 是设计的必然结果（折算：跟上生成速度需要每天 1,174 次交互）。
//
// 三条硬约束落在界面上：
//   1. 结果必须**显式选择**，下拉默认停在「无法判定」（indeterminate）。
//      **绝不能默认成「未发生」**：那是凭空给成百上千条命题盖上"没发生"的断言，
//      会直接进入 Brier 与命中率的分子分母，把校准彻底污染。
//   2. 确认前必须看到「将结算 N 条、结果 X、影响区间 Y」。
//   3. 结算不可撤销（resolutions 有不可变触发器），所以确认这一步不能省。
const BATCH_OUTCOMES = [
  ['indeterminate', '无法判定（默认｜不参与校准打分）'],
  ['occurred', '发生'],
  ['not_occurred', '未发生'],
  ['partial', '部分发生'],
];

function outcomeLabel(value) {
  const hit = BATCH_OUTCOMES.find(([key]) => key === value);
  return hit ? hit[1] : String(value || '');
}

function batchCard() {
  return `<h2 class="section-title u-mt-md">批量结算</h2>
  <section class="card">
    <p class="u-dim">按条件一次处理一批<strong>已到期</strong>的预测。默认结果是「无法判定」——它只清积压，不进命中率也不进 Brier。</p>
    <div class="u-row">
      <input type="text" id="batch-category" placeholder="类别（留空 = 全部）" aria-label="按类别筛选">
      <input type="date" id="batch-due-before" aria-label="到期早于这一天">
      <select id="batch-outcome" aria-label="批量结算结果" title="默认「无法判定」：只清理积压，不参与 Brier 与命中率。改成「未发生」等于替这批命题断言“没发生”——只在你真的逐条看过之后才改。">
        ${BATCH_OUTCOMES.map(([value, label], index) => `<option value="${value}"${index === 0 ? ' selected' : ''}>${label}</option>`).join('')}
      </select>
      <button type="button" class="btn btn-sm" id="batch-preview">先看将结算多少条</button>
      <button type="button" class="btn btn-sm btn-primary" id="batch-run" disabled>确认结算</button>
    </div>
    <div id="batch-preview-box" class="u-mt-sm u-dim">尚未预览。批量结算不可撤销，请先预览。</div>
  </section>`;
}

function bindBatch(root, refresh) {
  const categoryInput = root.querySelector('#batch-category');
  const dueInput = root.querySelector('#batch-due-before');
  const outcomeSelect = root.querySelector('#batch-outcome');
  const previewBtn = root.querySelector('#batch-preview');
  const runBtn = root.querySelector('#batch-run');
  const box = root.querySelector('#batch-preview-box');
  if (!previewBtn || !runBtn || !box) return;
  let previewed = null;
  const currentFilters = () => ({
    category: (categoryInput?.value || '').trim(),
    due_before: dueInput?.value || '',
    outcome: outcomeSelect?.value || 'indeterminate',
  });
  // 条件一变就作废上一次预览：绝不允许"预览的是 A、点下去的是 B"。
  const invalidate = () => { previewed = null; runBtn.disabled = true; };
  [categoryInput, dueInput, outcomeSelect].forEach(el => {
    el?.addEventListener('change', invalidate);
    el?.addEventListener('input', invalidate);
  });

  previewBtn.addEventListener('click', async () => {
    const filters = currentFilters();
    const query = new URLSearchParams();
    if (filters.category) query.set('category', filters.category);
    if (filters.due_before) query.set('due_before', filters.due_before);
    previewBtn.disabled = true;
    try {
      const data = await api(`/api/forecasts/batch-targets?${query.toString()}`);
      const count = Number(data?.count || 0);
      const span = (data?.window_end_min && data?.window_end_max)
        ? `${data.window_end_min} ~ ${data.window_end_max}` : '（无）';
      box.textContent = count
        ? `将结算 ${count} 条、结果「${outcomeLabel(filters.outcome)}」、影响到期区间 ${span}。确认后不可撤销。`
        : '按当前条件没有到期未结算的预测。';
      previewed = count ? filters : null;
      runBtn.disabled = !count;
    } catch (error) {
      showToast(`预览失败：${error.message}`, 'err');
      invalidate();
    } finally {
      previewBtn.disabled = false;
    }
  });

  runBtn.addEventListener('click', async () => {
    const filters = currentFilters();
    if (!previewed || previewed.outcome !== filters.outcome
        || previewed.category !== filters.category
        || previewed.due_before !== filters.due_before) {
      showToast('条件已变化，请重新预览', 'err');
      invalidate();
      return;
    }
    runBtn.disabled = true;
    try {
      const result = await api('/api/forecasts/batch-resolve', {
        method: 'POST',
        body: JSON.stringify({
          outcome: filters.outcome,
          due_before: filters.due_before,
          categories: filters.category ? [filters.category] : []
        })
      });
      showToast(`已结算 ${Number(result?.resolved_count || 0)} 条（结果：${outcomeLabel(filters.outcome)}）`);
      await refresh();
      invalidate();
      box.textContent = '上一批已完成。可继续按新条件预览。';
    } catch (error) {
      runBtn.disabled = false;
      showToast(`批量结算失败：${error.message}`, 'err');
    }
  });
}

// 账本每条的来源。必须能一眼看出哪些是"你选的"、哪些是系统自动填的 ——
// 否则校准分数里混着人类从未做过的预测，那个分数就没有意义。
function provenanceLabel(value) {
  const key = String(value || 'unknown');
  if (key === 'user') return '你选的';
  if (key === 'auto') return '系统自动';
  return '早期不明';
}

function kpiCard(label, value, dim = '') {
  return `<div class="card"><div class="kpi-label">${escapeHtml(label)}</div><div class="kpi-num ${dim}">${escapeHtml(value)}</div></div>`;
}

// Brier 周序列：数据驱动 SVG 折线（颜色全走 CSS 类，x/y 按数据范围归一）
function brierChartSvg(series) {
  const points = (Array.isArray(series) ? series : []).filter(p => Number.isFinite(Number(p?.brier)));
  if (points.length < 2) return '';
  const W = 640, H = 160, PAD = 28;
  const xs = points.map((_, i) => PAD + (i * (W - PAD * 2)) / (points.length - 1));
  const values = points.map(p => Number(p.brier));
  const min = Math.min(...values), max = Math.max(...values);
  const span = max - min || 1;
  const ys = values.map(v => H - PAD - ((v - min) / span) * (H - PAD * 2));
  const line = xs.map((x, i) => `${i ? 'L' : 'M'}${x.toFixed(1)} ${ys[i].toFixed(1)}`).join('');
  const base = (max + min) / 2;
  const yBase = H - PAD - ((base - min) / span) * (H - PAD * 2);
  const first = points[0]?.week || '';
  const last = points[points.length - 1]?.week || '';
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Brier 分数周趋势">
    <line class="axis" x1="${PAD}" y1="${H - PAD}" x2="${W - PAD}" y2="${H - PAD}"/>
    <line class="series-base" x1="${PAD}" y1="${yBase.toFixed(1)}" x2="${W - PAD}" y2="${yBase.toFixed(1)}"/>
    <path class="series-user" d="${line}"/>
    <text class="axis-label" x="${PAD}" y="${H - 8}">${escapeHtml(first)}</text>
    <text class="axis-label" x="${W - PAD}" y="${H - 8}" text-anchor="end">${escapeHtml(last)}</text>
  </svg>
  <div class="legend u-mt-sm"><span class="key"><span class="swatch series-user"></span>实际 Brier</span><span class="key"><span class="swatch series-base"></span>基线参考</span></div>`;
}

// 按类别准确率条形（data-width + CSSOM 写宽度，规避 CSP style-src 拦行内 style）
function byCategoryRows(byCategory) {
  const rows = Object.entries(byCategory || {})
    .filter(([, v]) => Number.isFinite(Number(v)))
    .sort((a, b) => Number(b[1]) - Number(a[1]));
  if (!rows.length) return '';
  return rows.map(([key, value]) => {
    const pct = Math.max(0, Math.min(100, Number(value) * 100));
    return `<div class="cat-row"><span>${escapeHtml(categoryLabel(key))}</span><span class="bar-track"><span class="bar-fill" data-width="${pct.toFixed(1)}"></span></span><span class="num">${pct.toFixed(1)}%</span></div>`;
  }).join('');
}

// 候选确认：九档概率选择 → POST /api/cognition/candidates/{id}/confirm（AC-08）
const PROBS = [95, 90, 80, 65, 50, 35, 20, 10, 5];
function candidatesHtml(candidates) {
  const list = Array.isArray(candidates) ? candidates : [];
  if (!list.length) return '';
  return `<h2 class="section-title u-mt-md">待确认候选预测</h2>
  <div class="card">${list.map(c => `<div class="candidate" data-id="${escapeHtml(c.id)}">
    <div class="u-flex1"><p>${escapeHtml(c.statement || c.summary || '候选预测')}</p>
    <p class="u-dim">${escapeHtml(categoryLabel(c.category))} · 截止 ${escapeHtml(formatLocalTime(c.window_end))}</p>
    ${c.settleable === false ? `<p class="text-warn">停在待补充：${escapeHtml(c.settle_block_reason || '命题不满足结算准入')}。补齐之后才会进账本。</p>` : ''}</div>
    <div class="u-row"><select aria-label="你判断这件事发生的概率" title="你判断这件事发生的概率：10% = 不太可能，50% = 五五开，90% = 几乎必然。选完后点确认，远见会在截止日检查是否真的发生。">
      ${PROBS.map(p => `<option value="${p}">${p}%</option>`).join('')}
    </select>
    <button type="button" class="btn btn-sm btn-primary" data-confirm title="把这个预测连同你选的概率一起记入预测账本。">确认</button></div>
  </div>`).join('')}</div>`;
}

// 预测账本表（分页）：每行可结算；已结算行置灰
function ledgerRows(forecasts) {
  const list = Array.isArray(forecasts) ? forecasts : [];
  return list.map(f => {
    const resolved = f.status === 'resolved';
    // 结算方式要一眼看得出来（R-01）：批量写出来的「无法判定」是"没来得及看"，
    // 逐条判定的「无法判定」是"看了但判不了"——两者含义完全不同。
    const resolvedByLabel = f.resolved_by === 'batch' ? '批量/自动'
      : (f.resolved_by === 'user' ? '逐条判定' : '早期不明');
    const resolveCell = resolved
      ? `<span class="u-dim">已结算（${escapeHtml(resolvedByLabel)}）</span>`
      : `<div class="u-row">
          <select aria-label="结算结果" data-outcome="${escapeHtml(f.forecast_id)}">
            <option value="occurred">发生</option>
            <option value="not_occurred">未发生</option>
            <option value="partial">部分发生</option>
            <option value="indeterminate">无法判定</option>
          </select>
          <input type="date" data-resolved-at="${escapeHtml(f.forecast_id)}" aria-label="结算日期">
          <button type="button" class="btn btn-sm" data-resolve="${escapeHtml(f.forecast_id)}">结算</button>
        </div>`;
    return `<tr class="${resolved ? 'is-resolved' : ''}" data-id="${escapeHtml(f.forecast_id)}">
      <td>${escapeHtml(String(f.statement || f.summary || '').slice(0, 80))}</td>
      <td>${escapeHtml(categoryLabel(f.category))}</td>
      <td class="num">${escapeHtml(f.probability != null ? `${(Number(f.probability) * 100).toFixed(0)}%` : '—')}</td>
      <td>${escapeHtml(provenanceLabel(f.confirmed_by))}</td>
      <td>${escapeHtml(statusLabel(f.status))}</td>
      <td>${escapeHtml(formatLocalTime(f.created_at))}</td>
      <td class="resolve-cell">${resolveCell}</td>
    </tr>`;
  }).join('');
}

// 结算按钮绑定：四选结果 + 日期 → POST /api/forecasts/{id}/resolve
function bindResolve(body) {
  body.querySelectorAll('[data-resolve]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const id = btn.dataset.resolve;
      const outcome = body.querySelector(`select[data-outcome="${CSS.escape(id)}"]`)?.value;
      const resolvedAt = body.querySelector(`input[data-resolved-at="${CSS.escape(id)}"]`)?.value || '';
      if (!outcome) { showToast('请选择结算结果', 'err'); return; }
      btn.disabled = true;
      try {
        await api(`/api/forecasts/${encodeURIComponent(id)}/resolve`, {
          method: 'POST',
          body: JSON.stringify({outcome, resolved_at: resolvedAt})
        });
        showToast('已结算');
        await paintLedger();
      } catch (error) {
        btn.disabled = false;
        showToast(`结算失败：${error.message}`, 'err');
      }
    });
  });
}

export async function render(root) {
  let calib = null;
  try {
    calib = await api('/api/calibration'); // 端点开发中：失败降级空态
  } catch (_) { calib = null; }

  const hit = Number(calib?.hit_rate);
  const fpr = Number(calib?.false_positive_rate);
  const brier = Number(calib?.brier);
  const hasStats = calib && [hit, fpr, brier].some(v => Number.isFinite(v));

  const kpis = hasStats
    ? kpiCard('命中率', Number.isFinite(hit) ? `${(hit * 100).toFixed(1)}%` : '—')
      + kpiCard('误报率', Number.isFinite(fpr) ? `${(fpr * 100).toFixed(1)}%` : '—')
      + kpiCard('Brier 分数', Number.isFinite(brier) ? brier.toFixed(3) : '—', 'u-dim')
      + kpiCard('已结算预测', Number.isFinite(Number(calib?.resolved_total)) ? String(calib.resolved_total) : '—')
    : kpiCard('命中率', '样本不足', 'u-dim') + kpiCard('误报率', '样本不足', 'u-dim')
      + kpiCard('Brier 分数', '样本不足', 'u-dim') + kpiCard('已结算预测', '0');

  const chart = brierChartSvg(calib?.brier_series);
  const cats = byCategoryRows(calib?.by_category);
  root.innerHTML = `<div class="u-max">
    ${HELP_HTML}
    <section class="grid-kpi">${kpis}</section>
    ${exclusionCard(calib)}
    ${sourceBreakdownCard(calib)}
    <h2 class="section-title u-mt-md">Brier 周趋势（≥8 周）</h2>
    <section class="card">${chart || NO_SAMPLE}</section>
    ${cats ? `<h2 class="section-title u-mt-md">按类别准确率</h2><section class="card u-row">${cats}</section>` : ''}
    <div id="calib-candidates">${candidatesHtml(calib?.candidates)}</div>
    ${batchCard()}
    <h2 class="section-title u-mt-md">预测账本（可结算）</h2>
    <section class="card"><div class="table-wrap"><table>
      <thead><tr><th>预测</th><th>类别</th><th>概率</th><th>来源</th><th>状态</th><th>创建</th><th>结算</th></tr></thead>
      <tbody id="ledger-body"></tbody>
    </table></div><div id="ledger-page"></div></section>
  </div>`;

  // 条形宽度 CSSOM 写入
  root.querySelectorAll('.bar-fill[data-width]').forEach(el => { el.style.width = `${el.dataset.width}%`; });

  // 候选确认绑定（candidate div 带 data-id，按钮带 data-confirm）
  root.querySelectorAll('.candidate[data-id]').forEach(row => {
    const btn = row.querySelector('[data-confirm]');
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        await api(`/api/cognition/candidates/${encodeURIComponent(row.dataset.id)}/confirm`, {
          method: 'POST',
          body: JSON.stringify({probability: Number(row.querySelector('select').value) / 100})
        });
        row.remove();
        render(root); // 确认后账本应出现不可变新版本
      } catch (error) {
        btn.disabled = false;
        showToast(`确认失败：${error.message}`, 'err');
      }
    });
  });

  // 预测账本（/api/forecasts 真实端点）
  const body = root.querySelector('#ledger-body');
  const pageBox = root.querySelector('#ledger-page');
  let state = {limit: 10, offset: 0};
  const paintLedger = async () => {
    try {
      const query = `?limit=${state.limit}&offset=${state.offset}`;
      const response = await api(`/api/forecasts${query}`);
      const forecasts = Array.isArray(response?.forecasts) ? response.forecasts : (Array.isArray(response) ? response : []);
      const total = Number(response?.total ?? forecasts.length) || forecasts.length;
      body.innerHTML = forecasts.length ? ledgerRows(forecasts)
        : `<tr><td colspan="6" class="u-dim">账本为空——确认候选预测后出现在这里。</td></tr>`;
      const range = pageRange(total, state.limit, state.offset);
      pageBox.innerHTML = paginationHtml(range);
      bindPagination(pageBox, '', dir => { state = {...state, offset: Math.max(0, state.offset + (dir === 'next' ? state.limit : -state.limit))}; paintLedger(); });
      bindResolve(body);
    } catch (error) {
      body.innerHTML = `<tr><td colspan="6" class="u-dim">账本读取失败：${escapeHtml(error.message)}</td></tr>`;
    }
  };
  await paintLedger();
  // 批量结算重画账本，但不整页重渲染——否则刚填好的筛选条件会被清掉。
  bindBatch(root, paintLedger);
}
