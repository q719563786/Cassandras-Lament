// 远见 v0.9 · API 封装 + 通用 UI 反馈 —— token 认证 / toast / loading / 错误面板 / 分页
import { yjIcon } from './icons.js';
import { pageRange } from './ui_core.js';

// 会话令牌：沿旧模式从 URL query 取（pywebview 打开时注入 ?token=）
const params = new URLSearchParams(location.search);
const TOKEN = params.get('token') || '';

// 统一请求：裸 JSON 响应（非 code/data 包裹）+ X-YuanJian-Token 头 + {error:{message}} 错误格式
export async function api(path, options = {}) {
  // 兜底：若调用方传入裸对象作为 body，自动序列化为 JSON，
  // 避免 fetch 把对象 toString 成 "[object Object]" 导致后端 json 解析失败。
  let body = options.body;
  if (body !== null && typeof body === 'object' &&
      !(body instanceof FormData) && !(body instanceof Blob) &&
      !(body instanceof ArrayBuffer) && typeof body.getReader !== 'function') {
    body = JSON.stringify(body);
  }
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
    throw new Error(message);
  }
  return payload;
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
