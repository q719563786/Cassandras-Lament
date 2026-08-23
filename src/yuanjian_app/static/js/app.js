// 远见 v1.1 · 启动装配 —— 图标注入 → 路由 → 顶栏动作 → 智能轮询 → 安全退出
import { yjMountIcons } from './icons.js';
import { api, showToast, withBusy, updateChrome } from './api.js';
import { initRouter, renderView, currentView } from './router.js';

// 静态壳里的 data-icon 占位一次性注入
yjMountIcons(document);

// ===== 侧栏导航：hash 跳转（router 监听 hashchange 完成渲染） =====
document.querySelectorAll('.nav-item[data-view]').forEach(btn => {
  btn.addEventListener('click', () => {
    location.hash = `#/${btn.dataset.view}`;
  });
});

// ===== 顶栏：手动刷新当前视图 =====
const runButton = document.getElementById('run-cognition');
runButton?.addEventListener('click', () => {
  const spinner = runButton.querySelector('svg');
  withBusy(runButton, '刷新中…', async () => {
    if (spinner) spinner.classList.add('spinning');
    try {
      await renderView();
      showToast('已刷新');
    } catch (error) {
      showToast(`刷新失败：${error.message}`, 'err');
    } finally {
      if (spinner) spinner.classList.remove('spinning');
    }
  });
});

// ===== 智能自动刷新 =====
// 仅在首页（today视图）且无弹窗打开时才刷新，避免打断用户操作
// 检查generated_at时间戳，数据未变化时不重渲染
let lastGeneratedAt = null;
let refreshTimer = null;

function isModalOpen() {
  return !!document.querySelector('.modal-backdrop');
}

async function smartRefresh() {
  // 不在首页、有弹窗打开、页面不可见时跳过
  const view = currentView();
  if (view !== 'today' || isModalOpen() || document.hidden) return;
  try {
    // 先轻量检查dashboard的generated_at是否变化
    const dash = await api('/api/risk-dashboard').catch(() => null);
    const newGeneratedAt = dash?.generated_at;
    if (newGeneratedAt && newGeneratedAt === lastGeneratedAt) return; // 数据未变，不重渲染
    lastGeneratedAt = newGeneratedAt;
    await renderView();
  } catch (_) {
    // 静默失败，下次再试
  }
}

// 首页每45秒检查一次新数据（后台研判约60秒一轮，留余量）
function startAutoRefresh() {
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(smartRefresh, 45000);
}
startAutoRefresh();

// 切换到首页时立即检查一次
window.addEventListener('hashchange', () => {
  if (currentView() === 'today') {
    lastGeneratedAt = null;
    setTimeout(smartRefresh, 500);
  }
});

// ===== 侧栏底部：安全退出（confirm 后 POST /api/shutdown） =====
document.getElementById('shutdown')?.addEventListener('click', () => {
  if (!window.confirm('安全退出远见？后台监控也会停止。')) return;
  api('/api/shutdown', {method: 'POST'}).catch(() => {}).finally(() => {
    showToast('正在退出，可以关闭本窗口了');
  });
});

// ===== 连接状态检查 =====
(async () => {
  try {
    await api('/api/cognition/status');
    updateChrome({connected: true});
  } catch (_) {
    updateChrome({connected: false});
  }
})();

// ===== 启动路由（默认 #/today） =====
if (!location.hash) location.hash = `#/${currentView()}`;
initRouter();
