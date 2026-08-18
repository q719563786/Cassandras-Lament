// 校准面板的使用说明：让非技术用户能读懂每个数字的含义。
// 折叠面板默认收起，标题直接告诉用户"为什么要做这件事"。
const HELP_HTML = `<details class="card calib-help u-mb-md">
    <summary class="section-title">如何使用这个面板？</summary>
    <div class="u-mt-sm body">
      <p>这个面板衡量<strong>你的判断</strong>准不准——不只是远见猜得准不准，而是远见帮你做出的判断、加上你自己的概率选择，最后有没有真的发生。</p>
      <ul>
        <li><strong>命中率</strong>：你确认过的预测里，真的发生了的比例。100% = 全部命中。</li>
        <li><strong>误报率</strong>：你确认过的预测里，事实没发生的比例。越低越好。</li>
        <li><strong>Brier 分数</strong>：衡量概率预测的准确性。0 = 完美，0.25 = 一般，越低越好。计算方法：把每次预测的"你给的概率 − 实际结果"平方后求平均。</li>
        <li><strong>已结算预测</strong>：观察期已结束、结果已记录的预测数量。这个数越多，上面三个数字越有参考价值。</li>
        <li><strong>候选预测的百分比</strong>：这是<strong>你判断它会发生的主观概率</strong>。从九个固定档位里选一个（5% / 10% / 20% / 35% / 50% / 65% / 80% / 90% / 95%），点"确认"后，远见会在截止日期检查它是否真的发生，并把你的概率和实际结果对比，记入 Brier 分数。</li>
      </ul>
      <p class="u-dim">一句话：选个概率 → 点确认 → 等到期看远见标对错。这是你训练自己判断力的方式。</p>
    </div>
  </details>`;

// 远见 v0.9 · 校准面板 —— KPI×4 / Brier 周序列 SVG / 按类别条形 / 候选确认 + 预测账本（AC-02/AC-08）
import { api, escapeHtml, showToast, paginationHtml, bindPagination, pageRange } from '../api.js';
import { statusLabel, categoryLabel, formatLocalTime } from '../ui_core.js';

// 无数据 / 端点未就绪：明示"样本不足"，绝不渲染 0 或假数（AC-02）
const NO_SAMPLE = `<div class="empty"><p>样本不足——已结算的预测还太少，等远见多跑几轮再看校准。</p></div>`;

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
    <p class="u-dim">${escapeHtml(categoryLabel(c.category))} · 截止 ${escapeHtml(formatLocalTime(c.window_end))}</p></div>
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
    const resolveCell = resolved
      ? `<span class="u-dim">已结算</span>`
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
    <h2 class="section-title u-mt-md">Brier 周趋势（≥8 周）</h2>
    <section class="card">${chart || NO_SAMPLE}</section>
    ${cats ? `<h2 class="section-title u-mt-md">按类别准确率</h2><section class="card u-row">${cats}</section>` : ''}
    <div id="calib-candidates">${candidatesHtml(calib?.candidates)}</div>
    <h2 class="section-title u-mt-md">预测账本（可结算）</h2>
    <section class="card"><div class="table-wrap"><table>
      <thead><tr><th>预测</th><th>类别</th><th>概率</th><th>状态</th><th>创建</th><th>结算</th></tr></thead>
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
}
