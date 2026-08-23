// 远见 v1.1 · hash 路由 —— #/today #/tell #/calib #/sources #/diag #/settings #/cluster/:id
import { showLoading, showPageError } from './api.js';
import * as today from './views/today.js';
import * as tell from './views/tell.js';
import * as calib from './views/calib.js';
import * as sources from './views/sources.js';
import * as diag from './views/diag.js';
import * as settings from './views/settings.js';
import * as cluster from './views/cluster.js';

const ROUTES = Object.freeze({
  today: {title: '今日远见', cap: 'DAILY BRIEF · 最高优先级', mod: today},
  calib: {title: '校准面板', cap: 'CALIBRATION · 预测准确率', mod: calib},
  sources: {title: '源管理', cap: 'SOURCES · 监听信道', mod: sources},
  diag: {title: '诊断中心', cap: 'DIAGNOSTICS · 系统体检', mod: diag},
  settings: {title: '设置', cap: 'SETTINGS · 本机配置', mod: settings},
  tell: {title: '告诉远见', cap: 'INPUT · 手动录入', mod: tell},
  cluster: {title: '事件详情', cap: 'EVENT DETAIL · 登高望远分析', mod: cluster}
});

export function currentView() {
  const raw = (location.hash || '').replace(/^#\/?/, '').split('?')[0];
  // 支持 #/cluster/:id 格式
  if (raw.startsWith('cluster/')) return 'cluster';
  return ROUTES[raw] ? raw : 'today';
}

export async function renderView() {
  const name = currentView();
  const route = ROUTES[name];
  const root = document.getElementById('view-root');
  if (!root || !route) return;
  document.querySelectorAll('.nav-item[data-view]').forEach(btn => {
    if (btn.dataset.view === name) btn.setAttribute('aria-current', 'page');
    else btn.removeAttribute('aria-current');
    if (btn.dataset.label) btn.setAttribute('aria-label', btn.dataset.label);
  });
  const title = document.getElementById('page-title');
  const cap = document.getElementById('page-caption');
  if (title) title.textContent = route.title;
  if (cap) cap.textContent = route.cap;
  showLoading(root, '正在接入…');
  try {
    await route.mod.render(root);
  } catch (error) {
    showPageError(root, `视图加载失败：${error.message || '未知错误'}`, () => renderView());
  }
}

export function navigate(name) {
  if (!ROUTES[name]) return;
  const target = `#/${name}`;
  if (location.hash === target) renderView();
  else location.hash = target;
}

export function initRouter() {
  window.addEventListener('hashchange', () => renderView());
  renderView();
}
