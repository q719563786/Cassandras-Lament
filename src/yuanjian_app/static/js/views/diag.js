// 远见 v1.0 · 诊断中心 —— 六瓦片聚合（AC-05：从未备份且自动备份关闭 = 琥珀告警）
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

  root.innerHTML = `<div class="u-max">
    <section class="grid-3">
      ${tileHtml({label: '源覆盖（启用 / 总数）', value: coverage, state: coverageState, icon: 'ic_antenna'})}
      ${tileHtml({label: '外部 AI', value: aiValue, state: aiState, icon: 'ic_gear'})}
      ${tileHtml({label: '数据库大小', value: dbValue, icon: 'ic_target'})}
      ${tileHtml({label: '上次备份', value: backupValue, state: backupState, icon: backupIcon})}
      ${tileHtml({label: '最近研判耗时', value: runValue, icon: 'ic_pulse'})}
      ${tileHtml({label: '运行时', value: String(diag?.runtime || '本机 127.0.0.1'), icon: 'ic_power'})}
    </section>
    ${neverWarned ? `<div class="note u-mt-md">从未备份且自动备份已关闭——重要判断历史存在丢失风险，建议到设置开启自动备份。</div>` : ''}
  </div>`;
}
