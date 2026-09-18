// 远见 v1.4.1 · API 封装 + 通用 UI 反馈 —— token 认证 / toast / loading / 错误面板 / 分页
import { yjIcon } from './icons.js';
import { pageRange } from './ui_core.js';

// 会话令牌：pywebview 打开时以查询参数注入。读到后立刻把地址栏里的令牌抹掉，
// 避免它留在窗口地址与浏览历史上（截图、地址复制都可能带出去）；
// 之后所有请求一律走 X-YuanJian-Token 头，不再依赖 URL。
const params = new URLSearchParams(location.search);
const TOKEN = params.get('token') || '';
if (TOKEN && typeof history !== 'undefined' && history.replaceState) {
  params.delete('token');
  const remaining = params.toString();
  history.replaceState(null, '', location.pathname + (remaining ? `?${remaining}` : '') + location.hash);
}

// 网络层失败判定：TypeError "Failed to fetch"（切网/开关代理的几秒离线窗口、
// 网络栈重置）属于可重试；HTTP 4xx/5xx 是服务器明确答复，不重试。
function isNetworkError(err) {
  return err?.name === 'TypeError' ||
    /failed to fetch|networkerror|network request failed|load failed/i.test(String(err?.message || ''));
}
const delay = (ms) => new Promise((r) => setTimeout(r, ms));
// navigator.onLine===false 时（切Wi-Fi/拨号/VPN握手），Chromium 连 127.0.0.1
// 也会直接失败；等待 online 事件或最多 maxMs 后继续。
function waitOnline(maxMs = 8000) {
  return new Promise((resolve) => {
    if (typeof navigator === 'undefined' || navigator.onLine !== false) return resolve();
    let done = false;
    const finish = () => { if (!done) { done = true; cleanup(); resolve(); } };
    const timer = setTimeout(finish, maxMs);
    window.addEventListener('online', finish, { once: true });
    function cleanup() { clearTimeout(timer); window.removeEventListener('online', finish); }
  });
}

// 统一请求：裸 JSON 响应（非 code/data 包裹）+ X-YuanJian-Token 头 + {error:{message}} 错误格式
// 网络层瞬时失败自动退避重试（500ms/1200ms/2500ms），覆盖切网、VPN 握手等几秒窗口；
// 本应用的写操作（确认/静音/误报）都是幂等的，安全可重试。
export async function api(path, options = {}) {
  // 兜底：若调用方传入裸对象作为 body，自动序列化为 JSON，
  // 避免 fetch 把对象 toString 成 "[object Object]" 导致后端 json 解析失败。
  let body = options.body;
  if (body !== null && typeof body === 'object' &&
      !(body instanceof FormData) && !(body instanceof Blob) &&
      !(body instanceof ArrayBuffer) && typeof body.getReader !== 'function') {
    body = JSON.stringify(body);
  }
  const backoff = [500, 1200, 2500];
  let lastError = null;
  for (let attempt = 0; attempt <= backoff.length; attempt++) {
    await waitOnline();
    try {
      const response = await fetch(path, {
        ...options,
        body,
        headers: {
          'Content-Type': 'application/json',
          'X-YuanJian-Token': TOKEN,
          ...(options.headers || {})
        }
      });
      const text = await response.text();
      let payload = null;
      try { payload = text ? JSON.parse(text) : null; } catch (_) { payload = null; }
      if (!response.ok) {
        const message = payload?.error?.message || `请求失败（${response.status}）`;
        throw new Error(message); // HTTP 明确答复：name=Error，不会被下面当网络错误重试
      }
      return payload;
    } catch (err) {
      lastError = err;
      if (!isNetworkError(err) || attempt === backoff.length) throw err;
      await delay(backoff[attempt]);
    }
  }
  throw lastError;
}

export function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}

// ===== toast（ic_ok / ic_warn 前缀图标） =====
let toastTimer = null;
export function showToast(message, kind = 'ok') {
  const box = document.getElementById('toast');
  if (!box) return;
  const icon = kind === 'err' ? yjIcon('ic_warn', 16) : yjIcon('ic_ok', 16);
  box.className = `toast toast-${kind === 'err' ? 'err' : 'ok'}`;
  box.innerHTML = `${icon}<span>${escapeHtml(message)}</span>`;
  box.hidden = false;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, 3800);
}

// ===== 视图级 loading（三方点动画） =====
export function showLoading(root, text = '正在读取…') {
  root.innerHTML = `<div class="loading-block" role="status"><span class="dot"></span><span class="dot"></span><span class="dot"></span>${escapeHtml(text)}</div>`;
}

// ===== 视图级错误面板（带重试） =====
export function showPageError(root, message, retry) {
  root.innerHTML = '';
  const panel = document.createElement('div');
  panel.className = 'state-panel';
  panel.innerHTML = `${yjIcon('ic_offline', 24, '离线')}<p>${escapeHtml(message)}</p>`;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'btn btn-sm';
  btn.textContent = '重试';
  btn.addEventListener('click', retry);
  panel.appendChild(btn);
  root.appendChild(panel);
}

// ===== 按钮忙碌态（innerHTML 保留图标结构，结束后还原） =====
export async function withBusy(button, busyText, action) {
  if (button.disabled) return null;
  const original = button.innerHTML;
  button.disabled = true;
  button.innerHTML = `${yjIcon('ic_refresh', 16)}<span>${escapeHtml(busyText)}</span>`;
  try {
    return await action();
  } finally {
    button.disabled = false;
    button.innerHTML = original; // 还原时连同 SVG 图标结构一并恢复
  }
}

// ===== 顶栏 chrome 同步：未读角标 + 连接监控点 =====
export function updateChrome({unread = 0, connected = null} = {}) {
  const badge = document.getElementById('unread');
  if (badge) {
    if (unread > 0) {
      badge.textContent = unread > 99 ? '99+' : String(unread);
      badge.hidden = false;
    } else {
      badge.hidden = true;
    }
  }
  const monitor = document.getElementById('connection');
  if (monitor && connected !== null) {
    monitor.classList.toggle('err', !connected);
    const dot = monitor.querySelector('.sdot');
    if (dot) dot.className = `sdot ${connected ? 'sdot-ok' : 'sdot-err'}`;
    const text = monitor.querySelector('.monitor-text');
    if (text) text.textContent = connected ? '后台监控 · 运行中' : '后台监控 · 已断开';
  }
}

// ===== 分页（渲染 + 绑定） =====
export function paginationHtml(range) {
  if (!range.total) return '';
  return `<div class="pagination" data-total="${range.total}">
    <button type="button" class="btn btn-sm" data-page="prev">上一页</button>
    <span>${range.start}-${range.end} / 共 ${range.total} 条</span>
    <button type="button" class="btn btn-sm" data-page="next">下一页</button>
  </div>`;
}

export function bindPagination(root, selector, onMove) {
  root.querySelectorAll(`${selector} [data-page]`).forEach(btn => {
    btn.addEventListener('click', () => onMove(btn.dataset.page));
  });
}

export { pageRange };
