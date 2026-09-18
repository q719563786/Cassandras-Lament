// 远见 v1.4.1 · 诊断中心 —— 六瓦片聚合（AC-05：从未备份且自动备份关闭 = 琥珀告警）
import { api, escapeHtml, showPageError } from '../api.js';
import { yjIcon } from '../icons.js';
import { formatBytes, formatLocalTime } from '../ui_core.js';

function tileHtml({label, value, state = '', icon = ''}) {
  return `<div class="diag-tile ${state ? `t-${state}` : ''}">
    <div class="label">${icon ? yjIcon(icon, 16) : ''}${escapeHtml(label)}</div>
    <div class="value">${escapeHtml(value)}</div>
  </div>`;
}

export async function render(root) {
  let diag = null;
  try {
    diag = await api('/api/diagnostics'); // 端点开发中：失败给重试面板
  } catch (_) {
    diag = null;
  }
  if (!diag) {
    showPageError(root, '诊断数据暂时读不到（后端能力可能仍在部署中）。', () => render(root));
    return;
  }

  // 源覆盖：启用/总
  const enabledCount = Number(diag?.sources_enabled);
  const totalCount = Number(diag?.sources_total);
  const coverage = [enabledCount, totalCount].every(Number.isFinite) ? `${enabledCount} / ${totalCount}` : '未知';
  const coverageState = totalCount > 0 && enabledCount === 0 ? 'err' : (Number.isFinite(enabledCount) && enabledCount > 0 ? 'ok' : 'warn');

  // AI：启用状态 + 今日用量 / 每日上限 + 限流退避中的作业数
  const aiEnabled = Boolean(diag?.ai_enabled);
  const aiJobs = Number(diag?.ai_jobs_today);
  const aiBudget = Number(diag?.ai_daily_budget);
  const aiQuota = Number.isFinite(aiBudget) ? ` / 上限 ${aiBudget}` : '';
  // 「今日已用 X / 上限 Y」看不出"是不是发太快了"。这个数持续 > 0，就说明对端
  // （Agnes 免费版 20 次/分钟）仍在我们 12 次/分钟的节流上限之下 —— 是用户把上限
  // 调到极限时唯一的反馈信号。字段缺失（旧后端）时当作 0，不改变原有显示。
  const aiRateLimited = Number(diag?.ai_rate_limit_pending);
  const rateLimited = Number.isFinite(aiRateLimited) && aiRateLimited > 0
    ? ` · 限流退避中 ${aiRateLimited}` : '';
  const aiValue = aiEnabled
    ? `已启用 · 今日 ${Number.isFinite(aiJobs) ? aiJobs : 0}${aiQuota} 次${rateLimited}`
    : '未启用（默认关闭）';
  const aiState = aiEnabled ? (rateLimited ? 'warn' : 'ok') : '';

  // DB 大小
  const dbBytes = Number(diag?.db_bytes);
  const dbValue = Number.isFinite(dbBytes) ? formatBytes(dbBytes) : '未知';

  // 上次备份：从未备份且自动备份关闭 → 琥珀告警（AC-05）
  const lastBackup = diag?.last_backup || null;
  const backupOn = Boolean(diag?.backup_enabled);
  const neverWarned = !lastBackup && !backupOn;
  const backupValue = lastBackup ? formatLocalTime(lastBackup) : '从未备份';
  const backupState = neverWarned ? 'warn' : (lastBackup ? 'ok' : 'warn');
  const backupIcon = lastBackup ? 'ic_ok' : 'ic_warn';

  // 最近研判耗时（容错显示）
  const elapsed = Number(diag?.last_run_ms);
  const runValue = Number.isFinite(elapsed) && elapsed > 0 ? `${(elapsed / 1000).toFixed(1)} 秒` : '暂无记录';

  // R-11：趋势探测器的健康度 —— rising 占**可判定**快照的比例。
  // 真库实测固定倍数口径下 rising 占 73%，而此前界面上看不到这个比例：
  // 一个多数时间在报警的探测器等价于没有报警。
  const health = diag?.trend_health || null;
  const risingShare = Number(health?.rising_share);
  const healthValue = (health && Number.isFinite(risingShare))
    ? `${(risingShare * 100).toFixed(1)}%（可判定 ${Number(health.judgeable || 0)} 个）`
    : '暂无采样';
  const healthState = health?.threshold_failed ? 'warn' : (Number.isFinite(risingShare) ? 'ok' : '');
  const healthNote = health?.threshold_failed
    ? `趋势阈值已失效：上升（rising）占可判定快照的 ${(risingShare * 100).toFixed(1)}%，超过 ${(Number(health.budget || 0.2) * 100).toFixed(0)}% 预算 —— 一个多数时间在报警的探测器等价于没有报警。`
    : '';

  // R-15：证据等级分布 + "官方来源是否标记过"。实测 E3/E4 从未出现，因为
  // primary_source 从未在任一信息源上标记过 —— 四级体系实际只跑两级，
  // 最窄的概率区间（E4 ±0.07）不可达。这件事必须说出来，不能等用户自己发现。
  const levels = diag?.evidence_levels || {};
  const levelKnown = ['E1', 'E2', 'E3', 'E4'].some(k => Number(levels[k] || 0) > 0);
  const levelValue = levelKnown
    ? ['E1', 'E2', 'E3', 'E4'].map(k => `${k} ${Number(levels[k] || 0)}`).join(' · ')
    : '暂无事件簇';
  const primaryCount = Number(diag?.primary_source_count || 0);
  const levelState = (levelKnown && primaryCount === 0) ? 'warn' : (levelKnown ? 'ok' : '');
  const evidenceNote = String(diag?.evidence_level_note || '');

  root.innerHTML = `<div class="u-max">
    <section class="grid-3">
      ${tileHtml({label: '源覆盖（启用 / 总数）', value: coverage, state: coverageState, icon: 'ic_antenna'})}
      ${tileHtml({label: '外部 AI', value: aiValue, state: aiState, icon: 'ic_gear'})}
      ${tileHtml({label: '数据库大小', value: dbValue, icon: 'ic_target'})}
      ${tileHtml({label: '上次备份', value: backupValue, state: backupState, icon: backupIcon})}
      ${tileHtml({label: '最近研判耗时', value: runValue, icon: 'ic_pulse'})}
      ${tileHtml({label: '运行时', value: String(diag?.runtime || '本机 127.0.0.1'), icon: 'ic_power'})}
      ${tileHtml({label: '趋势上升占比（rising）', value: healthValue, state: healthState, icon: 'ic_pulse'})}
      ${tileHtml({label: '证据等级分布（事件簇）', value: levelValue, state: levelState, icon: 'ic_target'})}
    </section>
    ${neverWarned ? `<div class="note u-mt-md">从未备份且自动备份已关闭——重要判断历史存在丢失风险，建议到设置开启自动备份。</div>` : ''}
    ${healthNote ? `<div class="note u-mt-md text-warn">${escapeHtml(healthNote)}</div>` : ''}
    ${evidenceNote ? `<div class="note u-mt-md text-warn">${escapeHtml(evidenceNote)}</div>` : ''}
  </div>`;
}
