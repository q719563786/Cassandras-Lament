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
const PROBS = [90, 80, 70, 60, 50, 40, 30, 20, 10];
function candidatesHtml(candidates) {
  const list = Array.isArray(candidates) ? candidates : [];
  if (!list.length) return '';
  return `<h2 class="section-title u-mt-md">待确认候选预测</h2>
  <div class="card">${list.map(c => `<div class="candidate" data-id="${escapeHtml(c.id)}">
    <div class="u-flex1"><p>${escapeHtml(c.statement || c.summary || '候选预测')}</p>
    <p class="u-dim">${escapeHtml(categoryLabel(c.category))} · 截止 ${escapeHtml(formatLocalTime(c.window_end))}</p></div>
    <div class="u-row"><select aria-label="确认概率">
      ${PROBS.map(p => `<option value="${p}">${p}%</option>`).join('')}
    </select>
    <button type="button" class="btn btn-sm btn-primary" data-confirm>确认</button></div>
  </div>`).join('')}</div>`;
}

// 预测账本只读表（分页）
function ledgerRows(forecasts) {
  const list = Array.isArray(forecasts) ? forecasts : [];
  return list.map(f => `<tr>
    <td>${escapeHtml(String(f.statement || f.summary || '').slice(0, 80))}</td>
    <td>${escapeHtml(categoryLabel(f.category))}</td>
    <td class="num">${escapeHtml(f.probability != null ? `${Number(f.probability).toFixed(0)}%` : '—')}</td>
    <td>${escapeHtml(statusLabel(f.status))}</td>
    <td>${escapeHtml(formatLocalTime(f.created_at))}</td>
  </tr>`).join('');
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
    <section class="grid-kpi">${kpis}</section>
    <h2 class="section-title u-mt-md">Brier 周趋势（≥8 周）</h2>
    <section class="card">${chart || NO_SAMPLE}</section>
    ${cats ? `<h2 class="section-title u-mt-md">按类别准确率</h2><section class="card u-row">${cats}</section>` : ''}
    <div id="calib-candidates">${candidatesHtml(calib?.candidates)}</div>
    <h2 class="section-title u-mt-md">预测账本（只读）</h2>
    <section class="card"><div class="table-wrap"><table>
      <thead><tr><th>预测</th><th>类别</th><th>概率</th><th>状态</th><th>创建</th></tr></thead>
      <tbody id="ledger-body"></tbody>
    </table></div><div id="ledger-page"></div></section>
  </div>`;

  // 条形宽度 CSSOM 写入
  root.querySelectorAll('.bar-fill[data-width]').forEach(el => { el.style.width = `${el.dataset.width}%`; });

  // 候选确认绑定
  root.querySelectorAll('.candidate[data-confirm]').forEach(row => {
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
        : `<tr><td colspan="5" class="u-dim">账本为空——确认候选预测后出现在这里。</td></tr>`;
      const range = pageRange(total, state.limit, state.offset);
      pageBox.innerHTML = paginationHtml(range);
      bindPagination(pageBox, '', dir => { state = {...state, offset: Math.max(0, state.offset + (dir === 'next' ? state.limit : -state.limit))}; paintLedger(); });
    } catch (error) {
      body.innerHTML = `<tr><td colspan="5" class="u-dim">账本读取失败：${escapeHtml(error.message)}</td></tr>`;
    }
  };
  await paintLedger();
}
