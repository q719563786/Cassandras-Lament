// 远见 v1.5.2 · 图标注册表 —— 14 枚自绘 SVG
// 唯一图标来源：HTML 写 <span data-icon="ic_radar"></span>，由 yjMountIcons 注入
//
// 设计语言（v1.5 重画）：**版画式印章**，不是插画。
//   参照密教模拟器／司辰之书那套"准则"符号的做派——对称、几何、像一枚刻在蜡上的印，
//   而不是一枚图标。全部 currentColor 描边、1.5 线宽、方角；只在"印点"处用极小实心，
//   且实心一律 fill="currentColor" stroke="none"（颜色随主题走）。
//   两点纪律：① 形状要能在 20px 下认出（信息量靠对称与留白，不靠细节）；
//             ② **不得改动键名**（ic_* 是 HTML 的 data-icon 契约，调用方不感知重绘）。
//
// 语义对照（新 → 旧）：
//   ic_radar   眼在环中（内在之眼）       ← 观察
//   ic_target  四芒星落于环内（印章）      ← 标定
//   ic_antenna 波纹 + 中轴 + 基座印点      ← 收听
//   ic_pulse   脉线 + 极端值印点           ← 趋势
//   ic_gear    钥匙（环身刻十字 = 符印）    ← 律法
//   ic_prompt  羽毛笔 + 墨点               ← 书写
//   ic_refresh 衔尾环 + 中心印点           ← 循环
//   ic_power   门 + 门槛线                 ← 关闭／退出
//   ic_msg     封蜡信笺                    ← 消息
//   ic_ok      印记圆环 + 勾               ← 确认
//   ic_warn    警示三角 + 竖直刻痕 + 点     ← 警告
//   ic_offline 闭合之眼（带睫）            ← 离线／暂停
//   ic_loading 三点成三角（∴ 式符印）      ← 加载
//   ic_atlas   古式浑天环（子午线 + 纬线）  ← 全球态势（新增，此前与 ic_radar 撞图）

export const ICONS = Object.freeze({
  ic_radar: '<circle cx="12" cy="12" r="9.2"/><path d="M5.8 12S8.6 8.2 12 8.2 18.2 12 18.2 12 15.4 15.8 12 15.8 5.8 12 5.8 12z"/><circle cx="12" cy="12" r="2.1"/><circle cx="12" cy="12" r=".8" fill="currentColor" stroke="none"/>',
  ic_target: '<circle cx="12" cy="12" r="9.2"/><path d="M12 4.6l1.5 5.9 5.9 1.5-5.9 1.5L12 19.4l-1.5-5.9L4.6 12l5.9-1.5z"/><circle cx="12" cy="12" r="1.1" fill="currentColor" stroke="none"/>',
  ic_antenna: '<path d="M12 4.2v13.6"/><circle cx="12" cy="19.6" r="1.1" fill="currentColor" stroke="none"/><path d="M8.2 7.6a6.2 6.2 0 0 0 0 8.8"/><path d="M15.8 7.6a6.2 6.2 0 0 1 0 8.8"/><path d="M5 4.6a10.4 10.4 0 0 0 0 14.8"/><path d="M19 4.6a10.4 10.4 0 0 1 0 14.8"/>',
  ic_pulse: '<path d="M2.2 12h3.6l2.2-6.6 4 13.2 2.2-6.6h4.6"/><circle cx="8" cy="5.4" r="1.1" fill="currentColor" stroke="none"/>',
  ic_gear: '<circle cx="7.8" cy="16.2" r="4"/><path d="M4.6 16.2h6.4"/><path d="M7.8 13v6.4"/><path d="M10.6 13.4L20.4 3.6"/><path d="M17.4 6.6l2.2 2.2"/><path d="M14.6 9.4l2.2 2.2"/>',
  ic_prompt: '<path d="M20.6 3.4c-6.6 0-9.8 3-11.8 5s-3 4-3 6l1.2 1.2c2.2 0 4.2-1.2 6.2-3.2s5.4-5.4 7.4-9z"/><path d="M3.4 20.6l3.6-3.6"/><circle cx="4.6" cy="19.4" r="1" fill="currentColor" stroke="none"/>',
  ic_refresh: '<path d="M20.4 12a8.4 8.4 0 1 1-2.9-6.3"/><path d="M18.9 3.2l2.7 2.9-4 1.5"/><circle cx="12" cy="12" r="1" fill="currentColor" stroke="none"/>',
  ic_power: '<path d="M6.6 3.4h10.8v17.2H6.6z"/><circle cx="14.4" cy="12" r="1.2" fill="currentColor" stroke="none"/><path d="M3.8 20.6h16.4"/>',
  ic_msg: '<rect x="3" y="5.4" width="18" height="13.2"/><path d="M3 5.4l9 6.6 9-6.6"/><circle cx="12" cy="12" r="1.9"/>',
  ic_ok: '<circle cx="12" cy="12" r="9.2"/><path d="M7.6 12.4l3.1 3.1 5.7-6.6"/>',
  ic_warn: '<path d="M12 3.6l9.2 17H2.8z"/><path d="M12 9.6v5.2"/><circle cx="12" cy="17.6" r="1" fill="currentColor" stroke="none"/>',
  ic_offline: '<path d="M3 12.8c2.4-3.6 5.4-5.4 9-5.4s6.6 1.8 9 5.4"/><path d="M6.4 13.2l-1.5 2.7"/><path d="M12 13.8v3.1"/><path d="M17.6 13.2l1.5 2.7"/>',
  ic_loading: '<circle cx="12" cy="6.6" r="1.7" fill="currentColor" stroke="none"/><circle cx="7.4" cy="16.2" r="1.7" fill="currentColor" stroke="none" opacity=".55"/><circle cx="16.6" cy="16.2" r="1.7" fill="currentColor" stroke="none" opacity=".25"/>',
  ic_atlas: '<circle cx="12" cy="12" r="9.2"/><path d="M12 2.8c2.7 2.6 2.7 15.8 0 18.4-2.7-2.6-2.7-15.8 0-18.4z"/><path d="M3.2 9.2h17.6"/><path d="M3.2 14.8h17.6"/>'
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
