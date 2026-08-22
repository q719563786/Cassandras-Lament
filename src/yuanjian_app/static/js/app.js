// 远见 v0.9 · 启动装配 —— 图标注入 → 路由 → 顶栏动作 → 监控轮询 → 安全退出
import { yjMountIcons } from './icons.js';
import { api, showToast, withBusy, updateChrome } from './api.js';
import { initRouter, renderView, currentView } from './router.js';
import { openDrawer, refreshUnread } from './views/notifications.js';

// 静态壳里的 data-icon 占位一次性注入
yjMountIcons(document);

// ===== 侧栏导航：hash 跳转（router 监听 hashchange 完成渲染） =====
document.querySelectorAll('.nav-item[data-view]').forEach(btn => {
  btn.addEventListener('click', () => {
    location.hash = `#/${btn.dataset.view}`;
  });
});

// ===== 顶栏：通知抽屉 =====
document.getElementById('open-notifications')?.addEventListener('click', () => openDrawer());

// ===== 顶栏：刷新（后台每60秒自动研判，前端每30秒自动刷新，按钮手动刷新当前视图） =====
const runButton = document.getElementById('run-cognition');
runButton?.addEventListener('click', () => {
  const spinner = runButton.querySelector('svg');
  withBusy(runButton, '刷新中…', async () => {
    if (spinner) spinner.classList.add('spinning');
    try {
      await renderView();          // 当前视图重拉数据
      await refreshUnread();       // 新通知可能产生
      showToast('已刷新');
    } catch (error) {
      showToast(`刷新失败：${error.message}`, 'err');
    } finally {
      if (spinner) spinner.classList.remove('spinning');
    }
  });
});

// 前端每30秒自动刷新当前视图（后台认知每60秒自动运行）
setInterval(async () => {
  try { await renderView(); } catch (_) { /* 静默失败，下次再试 */ }
}, 30000);

// ===== 侧栏底部：安全退出（confirm 后 POST /api/shutdown） =====
document.getElementById('shutdown')?.addEventListener('click', () => {
  if (!window.confirm('安全退出远见？后台监控也会停止。')) return;
  api('/api/shutdown', {method: 'POST'}).catch(() => {}).finally(() => {
    showToast('正在退出，可以关闭本窗口了');
  });
});

// ===== 连接状态 + 未读角标轮询 =====
(async () => {
  try {
    await api('/api/cognition/status');
    updateChrome({connected: true});
  } catch (_) {
    updateChrome({connected: false});
  }
  await refreshUnread();
})();
setInterval(refreshUnread, 60000);

// ===== 启动路由（默认 #/today，AC-01） =====
if (!location.hash) location.hash = `#/${currentView()}`;
initRouter();
