// 远见 v0.9 · 通知中心 —— 顶栏抽屉 + 未读角标同步（Spec §7：顶栏入口，不做一级路由）
import { api, escapeHtml, showToast, paginationHtml, bindPagination, pageRange } from '../api.js';
import { yjIcon } from '../icons.js';
import { statusLabel, formatLocalTime } from '../ui_core.js';

// 拉未读数，同步顶栏角标（app.js 每 60s 轮询；读不到静默降级）
export async function refreshUnread() {
  try {
    const response = await api('/api/notifications?limit=1&offset=0&status=unread');
    const total = Number(response?.total ?? response?.unread_count);
    const badge = document.getElementById('unread');
    if (badge && Number.isFinite(total)) {
      if (total > 0) {
        badge.textContent = total > 99 ? '99+' : String(total);
        badge.hidden = false;
      } else {
        badge.hidden = true;
      }
    }
  } catch (_) { /* 断连时保留旧角标，不打扰 */ }
}

function rowHtml(n) {
  return `<div class="notif-row ${n.status === 'unread' ? 'unread' : ''}" data-id="${escapeHtml(n.id)}">
    <p class="t">${escapeHtml(String(n.title || n.summary || '').slice(0, 120))}</p>
    <div class="u-row u-between"><span class="m">${escapeHtml(formatLocalTime(n.created_at))}</span>
    ${n.status === 'unread' ? '<button type="button" class="btn btn-sm" data-read>标为已读</button>' : ''}</div>
  </div>`;
}

// 打开抽屉：backdrop + 右侧面板（role=dialog；Esc / 点遮罩关闭）
export async function openDrawer() {
  document.querySelectorAll('.drawer-backdrop, .drawer').forEach(el => el.remove());
  const backdrop = document.createElement('div');
  backdrop.className = 'drawer-backdrop';
  const drawer = document.createElement('aside');
  drawer.className = 'drawer';
  drawer.setAttribute('role', 'dialog');
  drawer.setAttribute('aria-label', '通知中心');
  drawer.innerHTML = `
    <header>
      <h2 class="section-title">通知中心</h2>
      <div class="u-row">
        <button type="button" class="btn btn-sm" data-all>全部已读</button>
        <button type="button" class="icon-btn" data-close aria-label="关闭">${yjIcon('ic_power', 20)}</button>
      </div>
    </header>
    <div class="chips" data-filter>
      <button type="button" class="chip" data-f="unread" aria-pressed="true">未读</button>
      <button type="button" class="chip" data-f="all" aria-pressed="false">全部</button>
    </div>
    <div class="list" data-list></div>
    <div data-page></div>`;
  document.body.append(backdrop, drawer);

  const close = () => { backdrop.remove(); drawer.remove(); document.removeEventListener('keydown', onKey); };
  const onKey = (event) => { if (event.key === 'Escape') close(); };
  document.addEventListener('keydown', onKey);
  backdrop.addEventListener('click', close);
  drawer.querySelector('[data-close]').addEventListener('click', close);

  let state = {filter: 'unread', limit: 20, offset: 0};
  const list = drawer.querySelector('[data-list]');
  const pageBox = drawer.querySelector('[data-page]');

  const paintList = async () => {
    list.innerHTML = `<div class="loading-block" role="status"><span class="dot"></span><span class="dot"></span><span class="dot"></span>正在读取…</div>`;
    try {
      const query = `?limit=${state.limit}&offset=${state.offset}${state.filter === 'unread' ? '&status=unread' : ''}`;
      const response = await api(`/api/notifications${query}`);
      const items = Array.isArray(response?.notifications) ? response.notifications : [];
      const total = Number(response?.total ?? items.length) || items.length;
      if (!items.length) {
        list.innerHTML = `<div class="empty">${yjIcon('ic_ok', 24, '无未读')}<p>${state.filter === 'unread' ? '没有未读通知。' : '还没有通知。'}</p></div>`;
      } else {
        list.innerHTML = items.map(rowHtml).join('');
      }
      const range = pageRange(total, state.limit, state.offset);
      pageBox.innerHTML = paginationHtml(range);
      bindPagination(pageBox, '', dir => {
        state = {...state, offset: Math.max(0, state.offset + (dir === 'next' ? state.limit : -state.limit))};
        paintList();
      });
      // 单条已读
      list.querySelectorAll('[data-read]').forEach(btn => {
        btn.addEventListener('click', async () => {
          const id = btn.closest('.notif-row').dataset.id;
          try {
            await api(`/api/notifications/${encodeURIComponent(id)}/read`, {method: 'POST'});
            await paintList();
            await refreshUnread();
          } catch (e) { showToast(`标记失败：${e.message}`, 'err'); }
        });
      });
    } catch (e) {
      list.innerHTML = `<div class="empty"><p>通知读取失败：${escapeHtml(e.message)}</p></div>`;
    }
  };

  drawer.querySelectorAll('[data-filter] .chip').forEach(chip => {
    chip.addEventListener('click', () => {
      state = {filter: chip.dataset.f, limit: state.limit, offset: 0};
      drawer.querySelectorAll('[data-filter] .chip').forEach(c => c.setAttribute('aria-pressed', String(c === chip)));
      paintList();
    });
  });
  drawer.querySelector('[data-all]').addEventListener('click', async () => {
    try {
      await api('/api/notifications/read-all', {method: 'POST'});
      showToast('已全部标记为已读');
      await paintList();
      await refreshUnread();
    } catch (e) { showToast(`操作失败：${e.message}`, 'err'); }
  });

  await paintList();
}
