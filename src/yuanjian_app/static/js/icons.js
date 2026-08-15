// 远见 v0.9 · 图标注册表 —— 13 枚自绘 SVG（designer-phase1.md §1 代码直用）
// 唯一图标来源：HTML 写 <span data-icon="ic_radar"></span>，由 yjMountIcons 注入
// 全部 currentColor / 1.5 描边 / miter 方角；圆弧仅雷达/刷新/电源三处几何弧

export const ICONS = Object.freeze({
  ic_radar: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4.5"/><path d="M12 12L18.4 5.6"/><rect x="10.5" y="10.5" width="3" height="3" fill="currentColor" stroke="none"/>',
  ic_target: '<rect x="4" y="4" width="16" height="16"/><path d="M12 4v4M12 16v4M4 12h4M16 12h4"/><rect x="9" y="9" width="6" height="6"/>',
  ic_antenna: '<rect x="10" y="4" width="4" height="4"/><path d="M12 8v12"/><path d="M8 20h8"/><path d="M7 5.5L4.5 3M17 5.5L19.5 3M4.5 10.5L2 8M19.5 10.5L22 8"/>',
  ic_pulse: '<path d="M2 12h5l2-6 4 12 3-6h6"/>',
  ic_gear: '<path d="M9 3h6v3h3v3h3v6h-3v3h-3v3H9v-3H6v-3H3V9h3V6h3V3z"/><rect x="9" y="9" width="6" height="6"/>',
  ic_prompt: '<path d="M4 5l7 7-7 7"/><path d="M13 19h7"/>',
  ic_msg: '<rect x="3" y="4" width="18" height="16"/><path d="M7 9l3 2.5L7 14"/><path d="M12.5 14h5"/>',
  ic_refresh: '<path d="M20 12a8 8 0 1 1-2.34-5.66"/><path d="M20.5 3.5v3.5h-3.5"/>',
  ic_power: '<rect x="3" y="3" width="18" height="18"/><path d="M12 7v5"/><path d="M8.2 9.2a5.4 5.4 0 1 0 7.6 0"/>',
  ic_ok: '<rect x="4" y="4" width="16" height="16"/><path d="M8 12l3 3 5-6"/>',
  ic_warn: '<rect x="4" y="4" width="16" height="16"/><path d="M12 7.5v6"/><rect x="11.25" y="16" width="1.5" height="1.5" fill="currentColor" stroke="none"/>',
  ic_offline: '<rect x="10" y="4" width="4" height="4"/><path d="M12 8v12"/><path d="M8 20h8"/><path d="M3 3l18 18"/>',
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
