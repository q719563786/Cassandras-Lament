// 远见 v1.5.3 · 「全球态势」地图视图 —— Canvas-2D 自绘等距圆柱投影
//
// 数据只读来自两个已有接口：
//   GET /api/situation/layers   —— 图层清单 + 各态势源抓取状态
//   GET /api/situation/points   —— 按图层 / 时间窗取点
// 底图来自同源静态资源 /geo/ne_110m_land.geojson（Natural Earth 110m land，
// 127 个多边形 / 5143 个顶点）。
//
// 三条硬约束（本轮红线，写在最前面以免后来者"顺手"改坏）：
//  1) 绝不引地图库 / 瓦片服务 / CDN。CSP 只有 'self'，且离线终端不该依赖外网。
//     "地图"就是自己在 canvas 上把经纬度画成像素。
//  2) 绝不写内联 style / style 块。CSP 的 style-src 'self' 会直接拦掉内联样式，
//     页面会静默变成裸 HTML。所有视觉一律走 views.css 里的 .atlas-* 类，
//     canvas 的尺寸也只用 width/height 两个属性（不是 CSS）。
//  3) 只读：不写库、不触发抓取、不影响任何其他视图或数据链。
import { api, escapeHtml } from '../api.js';
import { formatLocalTime } from '../ui_core.js';

// 时间窗：与后端 situation_points 的 hours 白名单（1~168）对得上。
const WINDOWS = Object.freeze([
  { hours: 24, label: '24 小时' },
  { hours: 72, label: '72 小时' },
  { hours: 168, label: '7 天' },
]);

// 单次取点上限。后端硬上限 2000，这里留出余量并配合"只画视口内的点"。
const POINT_LIMIT = 1500;

// 画布上的操作提示。只此一处定义：初始渲染、交互后复位都用它，
// 避免出现两处文案各说各话。
const HINT = '滚轮缩放 · 拖拽平移 · 点击事件看详情';

// 图层 → CSS 类名。用静态映射而不是在 class 属性里拼字符串，是为了让
// "用到的类有没有样式定义"这条机械护栏能被测试真正检查到。
const LAYER_CLASS = Object.freeze({
  quake: 'atlas-layer-quake',
  volcano: 'atlas-layer-volcano',
  wildfire: 'atlas-layer-wildfire',
  storm: 'atlas-layer-storm',
  flood: 'atlas-layer-flood',
  drought: 'atlas-layer-drought',
  disaster: 'atlas-layer-disaster',
  other: 'atlas-layer-other',
});

// ⚠ 量级语义：**各图层的 magnitude 根本不是同一个量纲**。
//   地震是矩震级 M（3~9），风暴是风速（kt），火山是 VEI（0~8），
//   野火/洪水/干旱常常只是上游给的强度指数，甚至完全没有数值。
//   所以这里给每个图层配一份"量纲说明"，界面上必须**带量纲显示**，
//   绝不能让用户把 M6.2 和 60kt 放在一起比较大小。
const MAGNITUDE_SEMANTICS = Object.freeze({
  quake: { unit: '震级 M', prefix: 'M ' },
  volcano: { unit: '火山爆发指数 VEI', prefix: 'VEI ' },
  storm: { unit: '风速（节）', prefix: '' },
  wildfire: { unit: '火势强度（上游指数）', prefix: '' },
  flood: { unit: '洪水强度（上游指数）', prefix: '' },
  drought: { unit: '干旱等级（上游指数）', prefix: '' },
  disaster: { unit: '灾害强度（上游指数）', prefix: '' },
  other: { unit: '强度（上游指数）', prefix: '' },
});

// canvas 取不到主题变量时的兜底配色（与 tokens.css 保持一致）。正常路径会读
// 真实 CSS 变量，这样主题换了地图也跟着走，不用在地图里再写死一份颜色。
const PALETTE_FALLBACK = Object.freeze({
  bg: '#13151A',
  land: '#2A3040',
  landStroke: 'rgba(255,255,255,0.10)',
  grid: 'rgba(255,255,255,0.05)',
  axis: 'rgba(255,255,255,0.12)',
  selected: '#E8ECF2',
  quake: '#D45B6A',
  volcano: '#D48A50',
  wildfire: '#D48A50',
  storm: '#5890C4',
  flood: '#5890C4',
  drought: '#D4A054',
  disaster: '#D48A50',
  other: '#6B7483',
});

// ===========================================================================
// 纯函数区（零 DOM，可被 node 直接执行验证）
// ===========================================================================

function clamp(value, low, high) {
  return Math.max(low, Math.min(high, value));
}

function clamp01(value) {
  return clamp(Number(value) || 0, 0, 1);
}

function formatNumber(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '';
  return Number.isInteger(number) ? String(number) : number.toFixed(1);
}

// 预警等级 → 序数。GDACS 的 Green/Orange/Red 是**同量纲**的相对等级，
// 可以用来调点的大小；跨图层不行。
export function severityRank(severity) {
  const text = String(severity || '').trim().toLowerCase();
  if (text === 'red') return 2;
  if (text === 'orange') return 1;
  return 0;
}

// 该图层的量级口径（中文），界面上与数字一起出现。
export function magnitudeUnit(layer) {
  return (MAGNITUDE_SEMANTICS[layer] || MAGNITUDE_SEMANTICS.other).unit;
}

// 量级数值（含图层特定的前缀，如地震的 "M "、火山的 "VEI "）。
// 没有数值时返回空串 —— 绝不编数字。
//
// ⚠ 必须显式挡住 null / undefined / ''：`Number(null)` 是 0，`Number.isFinite(0)`
//   又是 true，所以"上游没给量级"会静默变成"量级 0"。这条是测试抓出来的真缺陷。
function hasMagnitude(magnitude) {
  return magnitude !== null && magnitude !== undefined && magnitude !== '';
}

export function magnitudeText(layer, magnitude) {
  if (!hasMagnitude(magnitude)) return '';
  const value = Number(magnitude);
  if (!Number.isFinite(value)) return '';
  const spec = MAGNITUDE_SEMANTICS[layer] || MAGNITUDE_SEMANTICS.other;
  return `${spec.prefix}${formatNumber(value)}`;
}

// 详情里一行就能读懂的"量级"表述：有数值带量纲；没数值就报预警等级；
// 都没有就诚实写"未量化"。
export function magnitudeLabel(layer, magnitude, severity) {
  const text = magnitudeText(layer, magnitude);
  if (text) return `${text} · ${magnitudeUnit(layer)}`;
  if (severity) return `${severity} 级预警`;
  return '未量化';
}

// 点半径：**只在同量纲内**反映数值大小。有统一量纲（地震/风暴/火山）按数值缩放；
// 其余图层没有统一量纲，退化为按预警等级缩放，避免"把指数当震级比大小"。
export function dotRadius(layer, magnitude, severity) {
  const value = hasMagnitude(magnitude) ? Number(magnitude) : NaN;
  if (layer === 'quake' && Number.isFinite(value)) {
    return 2.5 + clamp01((value - 2.5) / 5) * 3; // 震级 2.5~7.5 → 2.5~5.5px
  }
  if (layer === 'storm' && Number.isFinite(value)) {
    return 2.5 + clamp01((value - 20) / 100) * 3; // 风速 20~120kt → 2.5~5.5px
  }
  if (layer === 'volcano' && Number.isFinite(value)) {
    return 2.5 + clamp01(value / 5) * 3; // VEI 0~5 → 2.5~5.5px
  }
  return 2.5 + severityRank(severity) * 1.5;
}

// 等距圆柱投影：经度线性映射到 x、纬度线性映射到 y（y 轴向下，故取负号）。
// view = {lon, lat, scale}，lon/lat 是画布中心对应的经纬度，scale 是"每度多少像素"。
export function project(lon, lat, view, width, height) {
  return {
    x: width / 2 + (Number(lon) - view.lon) * view.scale,
    y: height / 2 - (Number(lat) - view.lat) * view.scale,
  };
}

// 投影的逆运算：画布像素 → 经纬度。缩放"以光标为锚点"时必须用它。
export function unproject(x, y, view, width, height) {
  return {
    lon: view.lon + (Number(x) - width / 2) / view.scale,
    lat: view.lat - (Number(y) - height / 2) / view.scale,
  };
}

// 全局视图：整张世界地图宽度刚好等于画布宽度。
export function defaultView(width, height) {
  const scale = (Number(width) || 360) / 360;
  return { lon: 0, lat: 0, scale, minScale: scale * 0.6, maxScale: scale * 64 };
}

export function clampScale(scale, view) {
  return clamp(Number(scale) || view.minScale, view.minScale, view.maxScale);
}

// 命中测试：返回半径内最近的点，没有就返回 null。半径随点大小外扩一点，
// 否则小点很难点中。
export function hitTest(points, x, y, view, width, height, threshold = 7) {
  let best = null;
  let bestDistance = Infinity;
  for (const point of points || []) {
    const lon = Number(point?.lon);
    const lat = Number(point?.lat);
    if (!Number.isFinite(lon) || !Number.isFinite(lat)) continue;
    const at = project(lon, lat, view, width, height);
    const distance = Math.hypot(at.x - x, at.y - y);
    const reach = Math.max(threshold, dotRadius(point.layer, point.magnitude, point.severity) + 4);
    if (distance <= reach && distance < bestDistance) {
      best = point;
      bestDistance = distance;
    }
  }
  return best;
}

// 图层开关过滤。
export function filterPoints(points, active) {
  if (!active || typeof active.has !== 'function') return [];
  return (points || []).filter((point) => active.has(String(point?.layer || 'other')));
}

export function buildPointsQuery({ hours = 24, layer = '', limit = POINT_LIMIT, bbox = '' } = {}) {
  const params = new URLSearchParams();
  params.set('hours', String(hours));
  params.set('limit', String(limit));
  if (layer) params.set('layer', layer);
  if (bbox) params.set('bbox', bbox);
  return `?${params.toString()}`;
}

// 单个源的新鲜度文案。
export function freshnessLabel(source, now = new Date()) {
  if (!source) return '状态未知';
  const status = String(source.last_status || '');
  if (status === 'ok') {
    return `正常 · ${formatLocalTime(source.last_success_at, now)}`;
  }
  if (status === 'error') {
    const failures = Number(source.consecutive_failures) || 0;
    const suffix = failures > 0 ? ` ×${failures}` : '';
    return `待重试${suffix} · ${formatLocalTime(source.last_attempt_at, now)}`;
  }
  return '尚未抓取';
}

// 全局新鲜度：取所有源里最近一次成功抓取的时间，并数出有几个源在报错。
// 明确区分"地图上没点"和"源挂了导致地图上没点"——后者才是要用户看的东西。
export function overallFreshness(sources, now = new Date()) {
  const list = Array.isArray(sources) ? sources : [];
  let newest = '';
  let failing = 0;
  for (const source of list) {
    if (source?.last_status && source.last_status !== 'ok') failing += 1;
    const stamp = String(source?.last_success_at || '');
    if (stamp && stamp > newest) newest = stamp;
  }
  return {
    label: newest ? formatLocalTime(newest, now) : '暂无成功抓取',
    failing,
    total: list.length,
  };
}

// 底图的陆块计数。用来向测试证明"真的解析到了 127 个陆块"，而不是"把整张图
// 当一条线画出来"。**数的是多边形（外环）**：内环是湖，不是陆块。
export function countLandPolygons(land) {
  let polygons = 0;
  for (const feature of land || []) {
    forEachExteriorRing(feature, () => { polygons += 1; });
  }
  return polygons;
}

// 只取每个多边形的**外环**（coordinates[0]）。Natural Earth 的陆地没有洞，
// 但真按"所有环"去画，遇到有洞的数据会把湖也填成陆地。
function forEachExteriorRing(feature, visit) {
  const geometry = feature?.geometry;
  if (!geometry) return;
  if (geometry.type === 'Polygon') {
    const rings = geometry.coordinates || [];
    if (rings.length) visit(rings[0]);
  } else if (geometry.type === 'MultiPolygon') {
    (geometry.coordinates || []).forEach((polygon) => {
      if (Array.isArray(polygon) && polygon.length) visit(polygon[0]);
    });
  }
}

// 读主题色。读不到（或被 CSP/测试环境挡住）时用兜底色，绝不抛异常。
export function readPalette() {
  try {
    if (typeof getComputedStyle !== 'function' || typeof document === 'undefined'
      || !document.documentElement) {
      return { ...PALETTE_FALLBACK };
    }
    const styles = getComputedStyle(document.documentElement);
    const read = (name) => String(styles.getPropertyValue(name) || '').trim();
    const pick = (name, key) => read(name) || PALETTE_FALLBACK[key];
    return {
      ...PALETTE_FALLBACK,
      bg: pick('--bg-canvas', 'bg'),
      land: pick('--bg-elevated', 'land'),
      landStroke: pick('--border-medium', 'landStroke'),
      selected: pick('--text-primary', 'selected'),
      quake: pick('--red', 'quake'),
      volcano: pick('--orange', 'volcano'),
      wildfire: pick('--orange', 'wildfire'),
      storm: pick('--blue', 'storm'),
      flood: pick('--blue', 'flood'),
      drought: pick('--amber', 'drought'),
      disaster: pick('--orange', 'disaster'),
      other: pick('--text-muted', 'other'),
    };
  } catch (_) {
    return { ...PALETTE_FALLBACK };
  }
}

// ===========================================================================
// 绘制（只吃 ctx + 数据，不碰 DOM 结构）
// ===========================================================================

function drawGraticule(ctx, view, width, height, palette) {
  ctx.strokeStyle = palette.grid;
  ctx.lineWidth = 0.5;
  for (let lon = -180; lon <= 180; lon += 30) {
    const top = project(lon, 90, view, width, height);
    const bottom = project(lon, -90, view, width, height);
    ctx.beginPath();
    ctx.moveTo(top.x, top.y);
    ctx.lineTo(bottom.x, bottom.y);
    ctx.stroke();
  }
  for (let lat = -60; lat <= 60; lat += 30) {
    const left = project(-180, lat, view, width, height);
    const right = project(180, lat, view, width, height);
    ctx.beginPath();
    ctx.moveTo(left.x, left.y);
    ctx.lineTo(right.x, right.y);
    ctx.stroke();
  }
  // 赤道 / 本初子午线加重一点，给"我在哪"一个参照。
  ctx.strokeStyle = palette.axis;
  ctx.lineWidth = 0.8;
  const equatorLeft = project(-180, 0, view, width, height);
  const equatorRight = project(180, 0, view, width, height);
  ctx.beginPath();
  ctx.moveTo(equatorLeft.x, equatorLeft.y);
  ctx.lineTo(equatorRight.x, equatorRight.y);
  ctx.stroke();
  const meridianTop = project(0, 90, view, width, height);
  const meridianBottom = project(0, -90, view, width, height);
  ctx.beginPath();
  ctx.moveTo(meridianTop.x, meridianTop.y);
  ctx.lineTo(meridianBottom.x, meridianBottom.y);
  ctx.stroke();
}

// 画陆地。返回真正落到画布上的环数，便于测试对账。
export function drawLand(ctx, land, view, width, height, palette) {
  ctx.fillStyle = palette.land;
  ctx.strokeStyle = palette.landStroke;
  ctx.lineWidth = 0.6;
  let drawn = 0;
  for (const feature of land || []) {
    forEachExteriorRing(feature, (ring) => {
      if (!Array.isArray(ring) || ring.length < 3) return;
      ctx.beginPath();
      let started = false;
      for (const position of ring) {
        const lon = Number(position?.[0]);
        const lat = Number(position?.[1]);
        if (!Number.isFinite(lon) || !Number.isFinite(lat)) continue;
        const at = project(lon, lat, view, width, height);
        if (!started) {
          ctx.moveTo(at.x, at.y);
          started = true;
        } else {
          ctx.lineTo(at.x, at.y);
        }
      }
      if (started) {
        ctx.closePath();
        ctx.fill();
        ctx.stroke();
        drawn += 1;
      }
    });
  }
  return drawn;
}

export function drawPoints(ctx, points, view, width, height, palette, selectedId = '') {
  let drawn = 0;
  for (const point of points || []) {
    const lon = Number(point?.lon);
    const lat = Number(point?.lat);
    if (!Number.isFinite(lon) || !Number.isFinite(lat)) continue;
    const at = project(lon, lat, view, width, height);
    if (at.x < -20 || at.y < -20 || at.x > width + 20 || at.y > height + 20) continue;
    const radius = dotRadius(point.layer, point.magnitude, point.severity);
    ctx.globalAlpha = 0.85;
    ctx.beginPath();
    ctx.arc(at.x, at.y, radius, 0, Math.PI * 2);
    ctx.fillStyle = palette[point.layer] || palette.other;
    ctx.fill();
    ctx.globalAlpha = 1;
    if (selectedId && point.event_id === selectedId) {
      ctx.beginPath();
      ctx.arc(at.x, at.y, radius + 3, 0, Math.PI * 2);
      ctx.strokeStyle = palette.selected;
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }
    drawn += 1;
  }
  return drawn;
}

export function drawScene(ctx, land, points, view, width, height, palette, selectedId = '') {
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = palette.bg;
  ctx.fillRect(0, 0, width, height);
  drawGraticule(ctx, view, width, height, palette);
  const landRings = drawLand(ctx, land, view, width, height, palette);
  const pointCount = drawPoints(ctx, points, view, width, height, palette, selectedId);
  return { landRings, pointCount };
}

// ===========================================================================
// DOM 装配
// ===========================================================================

function shellHtml() {
  return `<div class="atlas-wrap">
    <div class="atlas-map-col">
      <div class="card atlas-panel">
        <div class="atlas-toolbar">
          <div class="chips atlas-windows" role="group" aria-label="时间窗">
            ${WINDOWS.map((option) => `<button type="button" class="chip" data-hours="${option.hours}" aria-pressed="${option.hours === 24 ? 'true' : 'false'}">${option.label}</button>`).join('')}
          </div>
          <div class="u-row atlas-actions">
            <button type="button" class="btn btn-sm" data-atlas="reset">回到全局</button>
            <span class="atlas-count" data-role="count"></span>
          </div>
        </div>
        <canvas class="atlas-canvas" width="900" height="460" role="img" aria-label="全球态势地图：滚轮缩放、拖拽平移、点击事件查看详情"></canvas>
        <p class="atlas-readout" data-role="readout">${HINT}</p>
      </div>
      <div class="card atlas-panel">
        <div class="card-head"><div class="card-title">图层量级口径</div></div>
        <div class="atlas-legend" data-role="legend"></div>
      </div>
    </div>
    <aside class="atlas-side">
      <div class="card atlas-panel">
        <div class="card-head"><div class="card-title">图层</div></div>
        <div class="atlas-layers" data-role="layers"></div>
      </div>
      <div class="card atlas-panel">
        <div class="card-head"><div class="card-title">数据新鲜度</div></div>
        <div class="atlas-fresh" data-role="fresh"></div>
      </div>
      <div class="card atlas-panel">
        <div class="card-head"><div class="card-title">事件详情</div></div>
        <div class="atlas-detail" data-role="detail"></div>
      </div>
    </aside>
  </div>`;
}

function layersHtml(layers, active) {
  if (!layers.length) return '<div class="atlas-detail-empty">暂无图层数据。</div>';
  return layers.map((row) => `<button type="button" class="atlas-layer ${LAYER_CLASS[row.layer] || LAYER_CLASS.other}" data-layer="${escapeHtml(row.layer)}" aria-pressed="${active.has(row.layer) ? 'true' : 'false'}">
      <span class="atlas-layer-dot"></span>
      <span class="atlas-layer-name">${escapeHtml(row.name || row.layer)}</span>
      <span class="atlas-layer-count">${escapeHtml(String(row.count ?? 0))}</span>
    </button>`).join('');
}

function legendHtml(layers) {
  if (!layers.length) return '<div class="atlas-detail-empty">暂无图层数据。</div>';
  const head = '<div class="atlas-legend-head">量级按图层各自的口径显示，不跨图层比较大小。</div>';
  const rows = layers.map((row) => `<div class="atlas-legend-row">
      <span class="atlas-swatch ${LAYER_CLASS[row.layer] || LAYER_CLASS.other}"></span>
      <span class="atlas-legend-text">${escapeHtml(row.name || row.layer)}</span>
      <span class="atlas-legend-unit">${escapeHtml(magnitudeUnit(row.layer))}</span>
    </div>`).join('');
  return head + rows;
}

function freshHtml(sources) {
  const overall = overallFreshness(sources);
  const line = `<div class="atlas-fresh-overall">最近成功抓取 · ${escapeHtml(overall.label)}${overall.failing ? ` · ${overall.failing}/${overall.total} 个源需重试` : ''}</div>`;
  const rows = (sources || []).map((source) => `<div class="atlas-fresh-item">
      <span class="atlas-fresh-dot ${source.last_status === 'ok' ? 'is-ok' : 'is-warn'}"></span>
      <span class="atlas-fresh-label">${escapeHtml(source.name || source.source_id || '态势来源')}</span>
      <span class="atlas-fresh-state">${escapeHtml(freshnessLabel(source))}</span>
    </div>`).join('');
  return line + (rows || '<div class="atlas-detail-empty">暂无态势来源。</div>');
}

function detailHtml(point) {
  if (!point) {
    return '<div class="atlas-detail-empty">在地图上点一个事件，这里显示它的详情。</div>';
  }
  const rows = [
    ['图层', point.layer_name || point.layer || '其他'],
    ['量级', `${magnitudeLabel(point.layer, point.magnitude, point.severity)}（各图层量纲不同）`],
    ['等级', point.severity || ''],
    ['发生', formatLocalTime(point.occurred_at)],
    ['来源', point.source_name || ''],
  ].filter(([, value]) => value);
  const meta = rows.map(([label, value]) => `<div class="atlas-detail-row"><span class="atlas-detail-label">${escapeHtml(label)}</span><span class="atlas-detail-value">${escapeHtml(value)}</span></div>`).join('');
  const link = point.canonical_url
    ? `<a class="atlas-detail-link" href="${escapeHtml(point.canonical_url)}" target="_blank" rel="noreferrer">打开原始来源</a>`
    : '';
  return `<div class="atlas-detail-title">${escapeHtml(point.title || '未命名事件')}</div>
    <div class="atlas-detail-meta">${meta}</div>${link}`;
}

// 只在真的有 canvas 时才装配画布。测试用的最小 DOM 桩没有 getContext，
// 这里返回 null，后续绘制一律跳过（结构仍然渲染出来）。
function setupCanvas(root) {
  const canvas = root.querySelector('.atlas-canvas');
  if (!canvas || typeof canvas.getContext !== 'function') return null;
  const ctx = canvas.getContext('2d');
  if (!ctx) return null;
  const dpr = Number(typeof window !== 'undefined' && window ? window.devicePixelRatio : 1) || 1;
  const width = Number(canvas.clientWidth) || 900;
  const height = Number(canvas.clientHeight) || 460;
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
  if (typeof ctx.setTransform === 'function') ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { canvas, ctx, width, height };
}

function measure(canvas, scene) {
  const dpr = Number(typeof window !== 'undefined' && window ? window.devicePixelRatio : 1) || 1;
  const width = Number(canvas.clientWidth) || scene.width;
  const height = Number(canvas.clientHeight) || scene.height;
  if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
  }
  if (typeof scene.ctx.setTransform === 'function') {
    scene.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  scene.width = width;
  scene.height = height;
  return { width, height };
}

function localPoint(canvas, event) {
  const rect = typeof canvas.getBoundingClientRect === 'function'
    ? canvas.getBoundingClientRect()
    : { left: 0, top: 0 };
  return { x: Number(event.clientX) - Number(rect.left || 0), y: Number(event.clientY) - Number(rect.top || 0) };
}

export async function render(root) {
  root.innerHTML = shellHtml();

  const hooks = {
    readout: root.querySelector('[data-role="readout"]'),
    count: root.querySelector('[data-role="count"]'),
    legend: root.querySelector('[data-role="legend"]'),
    layers: root.querySelector('[data-role="layers"]'),
    fresh: root.querySelector('[data-role="fresh"]'),
    detail: root.querySelector('[data-role="detail"]'),
    windows: root.querySelector('.atlas-windows'),
  };

  const state = {
    hours: 24,
    active: new Set(),
    known: new Set(),
    initialized: false,
    selected: null,
    layers: [],
    sources: [],
    points: [],
  };

  const scene = setupCanvas(root);
  if (scene) state.view = defaultView(scene.width, scene.height);

  function resetView() {
    if (!scene) return;
    state.view = defaultView(scene.width, scene.height);
    paint();
  }

  function paint() {
    if (!scene) return;
    const palette = readPalette();
    const visible = filterPoints(state.points, state.active);
    drawScene(
      scene.ctx, state.land || [], visible, state.view,
      scene.width, scene.height, palette, state.selected ? state.selected.event_id : '',
    );
  }

  function paintChrome() {
    const visible = filterPoints(state.points, state.active);
    if (hooks.layers) hooks.layers.innerHTML = layersHtml(state.layers, state.active);
    if (hooks.legend) hooks.legend.innerHTML = legendHtml(state.layers);
    if (hooks.fresh) hooks.fresh.innerHTML = freshHtml(state.sources);
    if (hooks.detail) hooks.detail.innerHTML = detailHtml(state.selected);
    if (hooks.count) {
      hooks.count.textContent = `显示 ${visible.length} / ${state.points.length} 个事件`;
    }
    if (hooks.readout) hooks.readout.textContent = HINT;
    if (hooks.windows) {
      hooks.windows.querySelectorAll('[data-hours]').forEach((button) => {
        button.setAttribute(
          'aria-pressed',
          Number(button.dataset.hours) === state.hours ? 'true' : 'false',
        );
      });
    }
  }

  function syncLayers() {
    const keys = state.layers.map((row) => row.layer);
    if (!state.initialized) {
      keys.forEach((key) => state.active.add(key));
      state.initialized = true;
    } else {
      keys.forEach((key) => {
        if (!state.known.has(key)) state.active.add(key);
      });
    }
    state.known = new Set(keys);
  }

  async function loadPoints() {
    const query = buildPointsQuery({ hours: state.hours, limit: POINT_LIMIT });
    const payload = await api(`/api/situation/points${query}`).catch(() => null);
    state.points = Array.isArray(payload?.points) ? payload.points : [];
    if (state.selected && !state.points.some((p) => p.event_id === state.selected.event_id)) {
      state.selected = null;
    }
  }

  const [layersPayload, landPayload] = await Promise.all([
    api('/api/situation/layers').catch(() => null),
    api('/geo/ne_110m_land.geojson').catch(() => null),
  ]);
  state.layers = Array.isArray(layersPayload?.layers) ? layersPayload.layers : [];
  state.sources = Array.isArray(layersPayload?.sources) ? layersPayload.sources : [];
  state.land = Array.isArray(landPayload?.features) ? landPayload.features : [];
  syncLayers();
  await loadPoints();
  paintChrome();
  paint();

  // 复位按钮与画布无强依赖，单独绑定，保证没有 canvas 时它也仍然可用。
  const resetButton = root.querySelector('[data-atlas="reset"]');
  if (resetButton) resetButton.addEventListener('click', resetView);

  // ---- 时间窗 ----
  if (hooks.windows) {
    hooks.windows.addEventListener('click', async (event) => {
      const button = event.target && event.target.closest('[data-hours]');
      if (!button) return;
      const hours = Number(button.dataset.hours);
      if (!Number.isFinite(hours) || hours === state.hours) return;
      state.hours = hours;
      await loadPoints();
      paintChrome();
      paint();
    });
  }

  // ---- 图层开关 ----
  if (hooks.layers) {
    hooks.layers.addEventListener('click', (event) => {
      const button = event.target && event.target.closest('[data-layer]');
      if (!button) return;
      const layer = button.dataset.layer;
      if (state.active.has(layer)) state.active.delete(layer);
      else state.active.add(layer);
      paintChrome();
      paint();
    });
  }

  // ---- 画布交互：缩放 / 平移 / 悬停 / 点选 ----
  if (scene) {
    const canvas = scene.canvas;
    const drag = { active: false, x: 0, y: 0, lon: 0, lat: 0, moved: 0 };

    canvas.addEventListener('wheel', (event) => {
      event.preventDefault();
      const at = localPoint(canvas, event);
      const before = unproject(at.x, at.y, state.view, scene.width, scene.height);
      const factor = Math.exp(-Number(event.deltaY || 0) * 0.0015);
      state.view.scale = clampScale(state.view.scale * factor, state.view);
      const after = unproject(at.x, at.y, state.view, scene.width, scene.height);
      state.view.lon += before.lon - after.lon;
      state.view.lat += before.lat - after.lat;
      state.view.lon = clamp(state.view.lon, -180, 180);
      state.view.lat = clamp(state.view.lat, -85, 85);
      paint();
    }, { passive: false });

    canvas.addEventListener('pointerdown', (event) => {
      drag.active = true;
      drag.moved = 0;
      drag.x = Number(event.clientX);
      drag.y = Number(event.clientY);
      drag.lon = state.view.lon;
      drag.lat = state.view.lat;
      if (canvas.classList && canvas.classList.add) canvas.classList.add('is-dragging');
      if (typeof canvas.setPointerCapture === 'function') {
        try { canvas.setPointerCapture(event.pointerId); } catch (_) { /* 忽略 */ }
      }
    });

    canvas.addEventListener('pointermove', (event) => {
      const at = localPoint(canvas, event);
      if (drag.active) {
        const dx = Number(event.clientX) - drag.x;
        const dy = Number(event.clientY) - drag.y;
        drag.moved += Math.abs(dx) + Math.abs(dy);
        state.view.lon = clamp(drag.lon - dx / state.view.scale, -180, 180);
        state.view.lat = clamp(drag.lat + dy / state.view.scale, -85, 85);
        paint();
        return;
      }
      const hovered = hitTest(
        filterPoints(state.points, state.active), at.x, at.y,
        state.view, scene.width, scene.height,
      );
      if (hooks.readout) {
        hooks.readout.textContent = hovered
          ? `${hovered.title || '未命名事件'} · ${magnitudeLabel(hovered.layer, hovered.magnitude, hovered.severity)}`
          : HINT;
      }
    });

    const endDrag = (event) => {
      if (!drag.active) return;
      drag.active = false;
      if (canvas.classList && canvas.classList.remove) canvas.classList.remove('is-dragging');
      if (drag.moved > 4) return; // 是拖动而不是点选
      const at = localPoint(canvas, event);
      const hit = hitTest(
        filterPoints(state.points, state.active), at.x, at.y,
        state.view, scene.width, scene.height,
      );
      state.selected = hit || null;
      if (hooks.detail) hooks.detail.innerHTML = detailHtml(state.selected);
      paint();
    };
    canvas.addEventListener('pointerup', endDrag);
    canvas.addEventListener('pointercancel', endDrag);
    canvas.addEventListener('pointerleave', () => {
      drag.active = false;
      if (canvas.classList && canvas.classList.remove) canvas.classList.remove('is-dragging');
    });

    if (typeof window !== 'undefined' && window && typeof window.addEventListener === 'function') {
      window.addEventListener('resize', () => {
        measure(canvas, scene);
        state.view = defaultView(scene.width, scene.height);
        paint();
      });
    }
  }
}
