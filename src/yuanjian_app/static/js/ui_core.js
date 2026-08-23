// 远见 v0.9 · 纯逻辑核心（零 DOM，可 Node 独立验证）
// 继承旧 ui_core.js / cognition_ui.js / risk_ui.js 的全部纯函数逻辑，改 ES Module 导出

const evidence = Object.freeze({
  E1: 'E1 · 单一来源线索',
  E2: 'E2 · 多源互证',
  E3: 'E3 · 含官方来源的强证据',
  E4: 'E4 · 高强度公开证据'
});
const trends = Object.freeze({
  rising: '上升', stable: '稳定', falling: '下降',
  low_sample: '样本积累中', accumulating: '样本积累中'
});
const statuses = Object.freeze({
  active: '有效', unread: '未读', read: '已读', digest: '每日汇总',
  ok: '正常', error: '待重试', paused: '已暂停', stale: '内容陈旧',
  open: '进行中', resolved: '已结算', low: '低', medium: '中', high: '高'
});
const categories = Object.freeze({
  health: '健康', finance: '金融', employment: '就业', safety: '安全',
  policy: '政策', technology: '科技', general: '综合'
});
const sourceKinds = Object.freeze({
  rss: 'RSS / Atom', html_list: '公开网页列表', gdelt: 'GDELT 全球新闻索引',
  json_api: '公开数据接口'
});
const regions = Object.freeze({
  heyuan: '河源', guangdong: '广东', national: '全国', global: '全球'
});
// P4: 信源分级——T1官方源 / T2权威媒体 / T3聚合或一般 / T4未验证
const sourceTiers = Object.freeze({
  T1: 'T1 · 官方源', T2: 'T2 · 权威媒体', T3: 'T3 · 聚合或一般', T4: 'T4 · 未验证'
});

export function evidenceLabel(value) { return evidence[value] || String(value || '证据待确认'); }
export function trendLabel(value) { return trends[value] || String(value || '趋势未知'); }
export function statusLabel(value) { return statuses[value] || String(value || '状态未知'); }
export function categoryLabel(value) { return categories[value] || String(value || '综合'); }
export function sourceKindLabel(value) { return sourceKinds[value] || String(value || '公开来源'); }
export function regionLabel(value) {
  if (!value) return '未分区';
  return regions[value] || String(value);
}
// P4: 信源分级标签——未知/空值时按 T3 显示（与数据库默认一致）
export function tierLabel(value) {
  if (!value) return sourceTiers.T3;
  return sourceTiers[value] || String(value);
}

// 风险三档映射：L4→高 / L3→中 / L1-L2→低（对用户呈现中文档位 + 方角标签）
export function riskClass(level) {
  return ({L4: 'high', L3: 'mid'})[level] || 'low';
}
export function riskLabel(level) {
  return ({L4: '高风险', L3: '中风险'})[level] || '低风险';
}
export function riskTag(level) {
  const text = ({L4: 'HIGH', L3: 'MID'})[level] || 'LOW';
  return `<span class="tag tag-${riskClass(level)}">${text}</span>`;
}

// 本地时间相对描述（offsetMinutes 可注入，便于测试）
export function formatLocalTime(value, now = new Date(), offsetMinutes = -new Date().getTimezoneOffset()) {
  if (!value) return '时间未知';
  const point = new Date(value);
  if (Number.isNaN(point.getTime())) return '时间未知';
  const shift = offsetMinutes * 60 * 1000;
  const localPoint = new Date(point.getTime() + shift);
  const localNow = new Date(now.getTime() + shift);
  const day = Date.UTC(localPoint.getUTCFullYear(), localPoint.getUTCMonth(), localPoint.getUTCDate());
  const today = Date.UTC(localNow.getUTCFullYear(), localNow.getUTCMonth(), localNow.getUTCDate());
  const difference = Math.floor((today - day) / 86400000);
  const time = `${String(localPoint.getUTCHours()).padStart(2, '0')}:${String(localPoint.getUTCMinutes()).padStart(2, '0')}`;
  if (difference === 0) return `今天 ${time}`;
  if (difference === 1) return `昨天 ${time}`;
  if (difference > 1 && difference < 7) return `${difference} 天前`;
  return `${localPoint.getUTCFullYear()}-${String(localPoint.getUTCMonth() + 1).padStart(2, '0')}-${String(localPoint.getUTCDate()).padStart(2, '0')}`;
}

export function buildQuery(values) {
  const params = new URLSearchParams();
  Object.entries(values || {}).forEach(([key, value]) => {
    if (value !== '' && value !== null && value !== undefined && value !== false) {
      params.set(key, String(value));
    }
  });
  const text = params.toString();
  return text ? `?${text}` : '';
}

export function pageRange(total, limit, offset) {
  const start = total ? offset + 1 : 0;
  return {start, end: Math.min(total, offset + limit), total};
}

export function movePage(state, direction, total) {
  const step = direction === 'next' ? state.limit : -state.limit;
  const maximum = Math.max(0, Math.floor((Math.max(0, total) - 1) / state.limit) * state.limit);
  return {...state, offset: Math.min(maximum, Math.max(0, state.offset + step))};
}

export function sourceHealthLabel(source) {
  if (!source?.enabled) return '已暂停';
  if (source.stale) return '内容陈旧';
  return statusLabel(source.last_status);
}

// 研判运行摘要（继承 cognition_ui.summarizeCognitionRun）
function count(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? Math.floor(number) : 0;
}
export function summarizeRun(result) {
  const processed = count(result?.backfill?.processed);
  const queued = count(result?.queued);
  const judgments = count(result?.judgments?.succeeded);
  const impacts = count(result?.mapped_impacts);
  const notifications = count(result?.notifications_created);
  const seconds = (count(result?.elapsed_ms) / 1000).toFixed(1);
  if (processed + queued + judgments + impacts + notifications === 0) {
    return `运行完成（${seconds}秒）。尚无新增待处理信息——去源管理启用几个与你相关的信息源，或把新情况直接告诉远见。`;
  }
  return `运行完成：处理${processed}条信息，排队${queued}个事件，完成${judgments}次研判，形成${impacts}条利益影响，新增${notifications}条提醒（${seconds}秒）`;
}

// 手动输入结果解读（继承 risk_ui.inputResult）
export function inputResult(signal) {
  const item = signal || {};
  const risk = ({L4: '高风险', L3: '中风险', L2: '低风险', L1: '低风险'})[item.alert_level] || '待判断';
  return {
    advice: item.recommended_action || '先记录事实，暂不做不可逆决定。',
    risk
  };
}

// 字节数人性化（诊断中心 DB 大小）
export function formatBytes(value) {
  const size = Number(value);
  if (!Number.isFinite(size) || size < 0) return '未知';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  if (size < 1024 * 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
  return `${(size / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

// 稿C v2 · 研判来源统一标注（消灭"贴标签"——界面词由 judgments.provider 驱动）
// 所有展示 gyw 的视图（today/calib/通知卡片）一律调此函数，禁止视图内自写文案。
// tone → CSS 徽标样式：remote=磷绿实线 / template=琥珀虚线 / muted=灰点线。
export function sourceBadge(candidate) {
  const c = candidate || {};
  if (c.gyw_source === 'legacy-backfill') {
    return { text: '类目模板补全 · 重跑后覆盖', tone: 'muted' };
  }
  const provider = String(c.judgment_provider || '').trim();
  if (provider && provider !== 'local') {
    return { text: `AI 研判 · ${provider}`, tone: 'remote' };
  }
  if (provider === 'local') {
    return { text: '模板推断 · 非真实研判', tone: 'template' };
  }
  return { text: '占位模板 · 引擎未产出', tone: 'muted' };
}

// 研判来源徽标 HTML（统一渲染，配合 sourceBadge）
export function sourceBadgeHtml(candidate) {
  const { text, tone } = sourceBadge(candidate);
  return `<span class="judgment-source judgment-${tone}" title="${text}">${text}</span>`;
}
