// 远见 v1.5.0 · 图标注册表 —— 13 枚自绘 SVG（designer-phase1.md §1 代码直用）
// 唯一图标来源：HTML 写 <span data-icon="ic_radar"></span>，由 yjMountIcons 注入
// 全部 currentColor / 1.5 描边 / miter 方角；圆弧仅雷达/刷新/电源三处几何弧

export const ICONS = Object.freeze({
  ic_radar: '<path d="M1.8 12S5.5 5.6 12 5.6 22.2 12 22.2 12 18.5 18.4 12 18.4 1.8 12 1.8 12z"/><circle cx="12" cy="12" r="2.6"/><circle cx="12" cy="12" r=".9" fill="currentColor" stroke="none"/>',
  ic_target: '<path d="M12 2.6l2.4 6.8 6.8 2.6-6.8 2.4L12 21.4l-2.4-7L2.8 12l6.8-2.6z"/><circle cx="12" cy="12" r="1.4"/>',
  ic_antenna: '<path d="M12 3.4v17.2"/><path d="M7.6 7.4a6.4 6.4 0 0 0 0 9.2"/><path d="M16.4 7.4a6.4 6.4 0 0 1 0 9.2"/><path d="M4.4 4.4a10.6 10.6 0 0 0 0 15.2"/><path d="M19.6 4.4a10.6 10.6 0 0 1 0 15.2"/>',
  ic_pulse: '<path d="M2 12h4.4l2.4-6.4 4 12.8 2.4-6.4H22"/>',
  ic_gear: '<circle cx="8" cy="16" r="3.6"/><path d="M10.6 13.4L20.2 3.8"/><path d="M17.4 6.6l2.2 2.2"/><path d="M14.6 9.4l2.2 2.2"/>',
  ic_prompt: '<path d="M20.4 3.6c-6.4 0-9.6 3-11.6 5S5.8 12.6 5.8 14.6l1.2 1.2c2.2 0 4.2-1.2 6.2-3.2s5.4-5.2 7.2-9z"/><path d="M3.6 20.4l3.2-3.2"/>',
  ic_refresh: '<path d="M20 12a8 8 0 1 1-2.34-5.66"/><path d="M20.5 3.5v3.5h-3.5"/>',
  ic_power: '<path d="M7 3.4h7.6a3 3 0 0 1 3 3v11.2a3 3 0 0 1-3 3H7z"/><circle cx="14.6" cy="12" r="1.1" fill="currentColor" stroke="none"/>',
  ic_msg: '<rect x="3" y="5" width="18" height="14"/><path d="M3 5l9 7 9-7"/>',
  ic_ok: '<rect x="4" y="4" width="16" height="16"/><path d="M8 12l3 3 5-6"/>',
  ic_warn: '<path d="M12 3l9 17H3z"/><path d="M12 9v5"/><circle cx="12" cy="17" r="1" fill="currentColor" stroke="none"/>',
  ic_offline: '<path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6-10-6-10-6z"/><path d="M3 3l18 18"/>',
  ic_loading: '<rect x="4" y="10.5" width="3" height="3" fill="currentColor" stroke="none"/><rect x="10.5" y="10.5" width="3" height="3" fill="currentColor" stroke="none" opacity=".55"/><rect x="17" y="10.5" width="3" height="3" fill="currentColor" stroke="none" opacity=".25"/>'
});

// 生成一枚 SVG 图标 HTML；label 提供时输出可读语义，否则 aria-hidden
export function yjIcon(name, size = 20, label) {
  const body = ICONS[name] || '';
  const aria = label ? `role="img" aria-label="${label}"` : 'aria-hidden="true"';
  return `<svg class="ico-${size}" viewBox="0 0 24 24" ${aria}>${body}</svg>`;
}

// 扫描容器内 data-icon 占位并替换为真实 SVG（data-icon-size / data-icon-label 可选）
export function yjMountIcons(root = document) {
  root.querySelectorAll('[data-icon]').forEach(el => {
    const size = el.dataset.iconSize || 20;
    el.outerHTML = yjIcon(el.dataset.icon, size, el.dataset.iconLabel);
  });
}
