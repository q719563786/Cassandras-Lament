// 远见 v1.4 · 设置 —— 备份/保留/学习开关持久化 + 移动摘要导出 + 外部 AI 表单
import { api, escapeHtml, showToast } from '../api.js';

// 通用 toggle 行：GET 容错（端点未就绪显示未知）+ PUT 持久化
function toggleRow({key, name, desc, on}) {
  return `<div class="set-row">
    <div><p class="name">${escapeHtml(name)}</p><p class="desc">${escapeHtml(desc)}</p></div>
    <button type="button" class="toggle" role="switch" data-key="${key}"
      aria-checked="${on ? 'true' : 'false'}" aria-label="${escapeHtml(name)}"></button>
  </div>`;
}

async function putSetting(path, body) {
  await api(path, {method: 'PUT', body: JSON.stringify(body)});
}

// 远程 AI 的设置端点是 POST（不是 PUT）：单独一个写手，别把方法名当参数到处传。
async function postSetting(path, body) {
  await api(path, {method: 'POST', body: JSON.stringify(body)});
}

// 备份体积（诊断面板与用户手册都用"922 MB"这种量级说话，不用字节数）
function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return '大小未知';
  const units = ['KB', 'MB', 'GB', 'TB'];
  let size = bytes;
  let unit = -1;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return unit < 0 ? `${size} B` : `${size.toFixed(1)} ${units[unit]}`;
}

  // 远程 AI 当前状态文案（端点域名 + 模型 + 启用/密钥状态 + 频率 + 每日上限）
function aiStatusText(ai) {
  if (!ai) return '读取中…未知';
  const domain = (ai.endpoint || '').replace(/^https?:\/\//, '').split('/')[0] || '—';
  const state = ai.enabled ? '已启用' : '未启用';
  const key = ai.configured ? '密钥已配置' : '密钥未配置';
  const freqMap = {low: '低(每日21点)', medium: '中(每6小时)', high: '高(每15分钟)'};
  const freq = freqMap[ai.frequency] || '中(每6小时)';
  const budget = Number(ai.daily_budget);
  const quota = Number.isFinite(budget) ? (budget > 0 ? `${budget} 次/天` : '已关闭远程') : '—';
  return `${state} · 模型 ${ai.model || '—'} · 端点 ${domain} · ${key} · 频率 ${freq} · 每日上限 ${quota}`;
}

// 通用 toggle 行绑定：只绑定 data-key 匹配的开关，避免重复绑定。
// 点的当下就落库：成功 toast、失败回滚开关并显示真实原因（绝不静默）。
// options.send：落库用的写手，默认 putSetting（远程 AI 那个端点是 POST，传 postSetting）。
// options.after：保存成功后的跟进动作（如回读状态行）。它失败**不**回滚开关 ——
// 设置确实已经落库了，回滚才是骗人。
async function bindToggle(root, path, key, extra, options = {}) {
  const toggle = root.querySelector(`.toggle[data-key="${key}"]`);
  if (!toggle) return;
  const {send = putSetting, after} = options;
  toggle.addEventListener('click', async () => {
    const next = toggle.getAttribute('aria-checked') !== 'true';
    toggle.setAttribute('aria-checked', String(next));
    try {
      const body = {enabled: next, ...(extra ? extra(next, root) : {})};
      await send(path, body);
      showToast('设置已保存');
    } catch (error) {
      toggle.setAttribute('aria-checked', String(!next)); // 失败回滚，不静默
      showToast(`保存失败：${error.message}`, 'err');
      return;
    }
    if (after) {
      try { await after(next); } catch (_) { /* 已保存成功，只是状态行没刷新 */ }
    }
  });
}

export async function render(root) {
  // 设置端点均在开发中：逐个 catch 降级为"未知"，不阻塞页面
  const [backup, retention, learning, archive, ai, interests] = await Promise.all([
    api('/api/settings/backup').catch(() => null),
    api('/api/settings/retention').catch(() => null),
    api('/api/settings/learning').catch(() => null),
    api('/api/settings/forecast-archive').catch(() => null),
    api('/api/settings/ai').catch(() => null),
    api('/api/interests').catch(() => null)
  ]);

  const hours = Array.from({length: 24}, (_, h) => `<option value="${h}"${Number(backup?.hour) === h ? ' selected' : ''}>${String(h).padStart(2, '0')}:00</option>`).join('');
  // 读取失败时退到后端默认值 7，不要让输入框显示 NaN
  const keepCount = Number(backup?.keep) > 0 ? Number(backup.keep) : 7;

  root.innerHTML = `<div class="u-max">
    <section class="set-sec">
      <h2>自动备份</h2>
      <div class="card">
        ${toggleRow({key: 'backup', name: '每日自动备份', desc: `跨过目标时段后产出 backups/ 新备份，滚动保留 ${keepCount} 份`, on: Boolean(backup?.enabled)})}
        <div class="set-row">
          <div><p class="name">目标时段</p><p class="desc">${backup ? '每天在这个时段附近执行一次' : '读取中…未知'}</p></div>
          <div class="u-row">
            <select class="btn btn-sm" data-backup-hour ${backup ? '' : 'disabled'} aria-label="备份目标时段">${hours}</select>
            <button type="button" class="btn btn-sm" data-backup-now ${backup ? '' : 'disabled'}>立即备份</button>
          </div>
        </div>
        <p class="u-dim u-mt-sm" data-backup-result hidden></p>
        <div class="set-row">
          <div><p class="name">备份保留份数（1–30，越多占磁盘越多）</p><p class="desc">${backup ? `当前保留 ${keepCount} 份` : '读取中…未知'} · 只清理超出的最旧自动备份，升级前的手工快照永不删除</p></div>
          <div class="field"><label for="backup-keep" class="sr-only">备份保留份数</label>
          <input id="backup-keep" type="number" min="1" max="30" value="${escapeHtml(String(keepCount))}" ${backup ? '' : 'disabled'}></div>
        </div>
      </div>
    </section>

    <section class="set-sec">
      <h2>数据保留</h2>
      <div class="card">
        ${toggleRow({key: 'retention', name: '自动清理过期数据', desc: '超过保留天数的原始抓取条目会被删除', on: Boolean(retention?.enabled)})}
        <div class="set-row">
          <div><p class="name">原始条目保留天数</p><p class="desc">${retention ? `当前 ${retention.days ?? 60} 天` : '读取中…未知'}</p></div>
          <div class="field"><label for="retention-days" class="sr-only">天数</label>
          <input id="retention-days" type="number" min="7" max="365" value="${escapeHtml(String(retention?.days ?? 60))}" ${retention ? '' : 'disabled'}></div>
        </div>
        <div class="set-row">
          <div><p class="name">结论明细保留天数</p><p class="desc">${retention ? `当前 ${retention.cluster_days ?? 180} 天` : '读取中…未知'} · 只清利益影响、通知、实体等明细；事件簇与研判永久保留</p></div>
          <div class="field"><label for="retention-cluster-days" class="sr-only">结论明细天数</label>
          <input id="retention-cluster-days" type="number" min="30" max="730" value="${escapeHtml(String(retention?.cluster_days ?? 180))}" ${retention ? '' : 'disabled'}></div>
        </div>
      </div>
    </section>

    <section class="set-sec">
      <h2>反馈学习</h2>
      <div class="card">
        ${toggleRow({key: 'learning', name: '误报反馈学习闭环', desc: '误报标记回灌：6 小时内对相应源降权（下限 0.2）', on: Boolean(learning?.enabled)})}
      </div>
    </section>

    <section class="set-sec">
      <h2>预测闭环</h2>
      <div class="card">
        <div class="set-row">
          <div><p class="name">到期自动归档</p><p class="desc">超过 N 天仍未结算的预测，自动记为「无法判定」并归档。<strong>默认关闭</strong>——批量写入的归档结果不可撤销，所以这个开关只能由你打开。开启后只清理积压：结果不参与命中率与 Brier 打分。</p></div>
          <button type="button" class="toggle" role="switch" data-key="forecast-archive"
            aria-checked="${archive?.enabled ? 'true' : 'false'}" aria-label="到期自动归档"></button>
        </div>
        <div class="set-row">
          <div><p class="name">到期满多少天后归档（7–180）</p><p class="desc">${archive ? `当前 ${archive.days ?? 30} 天` : '读取中…未知'}</p></div>
          <div class="field"><label for="archive-days" class="sr-only">天数</label>
          <input id="archive-days" type="number" min="7" max="180" value="${escapeHtml(String(archive?.days ?? 30))}" ${archive ? '' : 'disabled'}></div>
        </div>
      </div>
    </section>

    <section class="set-sec">
      <h2>移动摘要</h2>
      <div class="card">
        <div class="set-row">
          <div><p class="name">导出今日只读摘要</p><p class="desc">生成自包含 HTML 到本机 mobile/ 目录，可传手机离线阅读</p></div>
          <button type="button" class="btn btn-primary btn-sm" data-export>立即导出</button>
        </div>
        <p class="u-dim u-mt-sm" data-export-path hidden></p>
      </div>
    </section>

    <section class="set-sec">
      <h2>外部 AI</h2>
      <div class="card">
        <div class="set-row">
          <div><p class="name">启用远程 AI</p><p class="desc">开启后远见用你填的模型做外部研判，密钥只存本机</p></div>
          <button type="button" class="toggle" role="switch" data-key="ai" data-ai-enabled
            aria-checked="${ai?.enabled ? 'true' : 'false'}" aria-label="启用远程AI"></button>
        </div>
        <div class="set-row">
          <div><p class="name">分析频率</p><p class="desc">低=每天21点汇总一次 · 中=每6小时 · 高=每15分钟</p></div>
          <div class="u-row" role="radiogroup" aria-label="AI分析频率">
            <button type="button" class="btn btn-sm ${ai?.frequency === 'low' ? 'btn-primary' : ''}" data-freq="low">低</button>
            <button type="button" class="btn btn-sm ${ai?.frequency === 'medium' ? 'btn-primary' : ''}" data-freq="medium">中</button>
            <button type="button" class="btn btn-sm ${ai?.frequency === 'high' ? 'btn-primary' : ''}" data-freq="high">高</button>
          </div>
        </div>
        <form data-ai-form>
          <div class="field u-mb-md"><label for="ai-endpoint">API 地址</label>
          <input id="ai-endpoint" name="endpoint" type="url" placeholder="https://…（留空 = 不启用）" value="${escapeHtml(String(ai?.endpoint || ''))}"></div>
          <div class="field u-mb-md"><label for="ai-model">模型编号</label>
          <input id="ai-model" name="model" type="text" placeholder="如 gpt-4o / claude-3-5-sonnet" value="${escapeHtml(String(ai?.model || ''))}"></div>
          <div class="field u-mb-md"><label for="ai-key">API 密钥</label>
          <input id="ai-key" name="token" type="password" placeholder="留空 = 不修改已存密钥"></div>
          <div class="field u-mb-md"><label for="ai-daily-budget">远程 AI 每日上限（0–100000）</label>
          <input id="ai-daily-budget" name="daily_budget" type="number" min="0" max="100000" required value="${escapeHtml(String(ai?.daily_budget ?? 2000))}">
          <p class="u-dim u-mt-sm">每天最多让远程 AI 研判多少条事件；填 0 = 关闭远程，只在本机研判。默认 2000。</p></div>
          <div class="u-row u-mb-md">
            <button type="button" class="btn btn-sm btn-secondary" data-ai-preset-agnes>一键填入 Agnes AI（免费）</button>
          </div>
          <p class="u-dim u-mt-sm" data-ai-status></p>
          <div class="u-end"><button type="submit" class="btn btn-primary">保存</button></div>
        </form>
      </div>
    </section>

    <section class="set-sec">
      <h2>个人利益登记</h2>
      <div class="card">
        <form data-interest-form class="u-mb-md">
          <div class="field u-mb-md"><label for="int-name">名称</label>
          <input id="int-name" name="name" type="text" placeholder="如 房贷 / 孩子升学" maxlength="40"></div>
          <div class="field u-mb-md"><label for="int-cat">类别</label>
          <select id="int-cat" name="category">
            <option value="health">健康安全</option>
            <option value="cashflow">现金流</option>
            <option value="work">工作收入</option>
            <option value="policy">政策权益</option>
            <option value="family">家庭关系</option>
            <option value="assets">资产负债</option>
            <option value="opportunity">机会成长</option>
          </select></div>
          <div class="field u-mb-md"><label for="int-imp">重要程度（1-5）</label>
          <input id="int-imp" name="importance" type="number" min="1" max="5" value="3"></div>
          <div class="field u-mb-md"><label for="int-priv">隐私级别</label>
          <select id="int-priv" name="privacy_level">
            <option value="P1">P1（仅本机）</option>
            <option value="P2">P2（脱敏）</option>
          </select></div>
          <div class="u-end"><button type="submit" class="btn btn-primary btn-sm">登记</button></div>
        </form>
        <div class="interest-list" id="interest-list"></div>
      </div>
    </section>

    <section class="set-sec">
      <h2>快捷入口</h2>
      <div class="card">
        <div class="set-row"><p class="name">行动雷达</p><button type="button" class="btn btn-sm" data-go-today>查看今日风险</button></div>
        <div class="set-row"><p class="name">校准面板</p><button type="button" class="btn btn-sm" data-go-calib>确认预测 / 复盘</button></div>
        <div class="set-row"><p class="name">告诉远见</p><button type="button" class="btn btn-sm" data-go-tell>手动录入新情况</button></div>
      </div>
    </section>

    <section class="set-sec">
      <h2>关于</h2>
      <div class="card">
        <div class="set-row">
          <div><p class="name">当前版本</p><p class="desc" data-app-version>读取中…</p></div>
          <button type="button" class="btn btn-sm" data-check-update>检查更新</button>
        </div>
        <p class="u-dim u-mt-sm">检查更新会访问一次 GitHub 获取公开发布信息，只读取版本号，不上传本机任何数据。</p>
        <p class="u-dim u-mt-sm" data-update-status></p>
      </div>
    </section>
  </div>`;

  // 三组开关各自绑定到对应端点（key 过滤，互不串扰）
  // 备份的每个字段 change 时都把另一个一起带上（显式读 DOM），避免"改一个把另一个
  // 打回默认"。注意 bindToggle 拼体是 {enabled: next, ...extra()}，extra 在后、会覆盖
  // enabled —— 所以 toggle 用的 extra 里**不能**再带 enabled，否则开关永远写回旧值。
  //
  // 时段/份数这两条路径同样**不带** enabled：这里原本写的是 `Boolean(backup?.enabled)`，
  // 而 backup 是**页面加载时**读到的快照 —— 用户先点开「每日自动备份」，再去改目标时段，
  // 就会把那一瞬间的旧值 false 一起写回去，开关被悄悄关掉（备份从此不再跑）。
  // 后端 write_backup_setting 在缺该键时沿用当前值，所以不带正是我们要的。
  const backupPayload = (hour, keep) => ({
    hour: Number(hour),
    keep: Number(keep),
  });
  bindToggle(root, '/api/settings/backup', 'backup', (next, r) => ({
    hour: Number(r.querySelector('[data-backup-hour]')?.value ?? 3),
    keep: Number(r.querySelector('#backup-keep')?.value ?? 7),
  }));
  root.querySelector('[data-backup-hour]')?.addEventListener('change', async (event) => {
    try {
      await putSetting('/api/settings/backup', backupPayload(
        event.target.value,
        root.querySelector('#backup-keep')?.value ?? 7,
      ));
      showToast('目标时段已保存');
    } catch (e) { showToast(`保存失败：${e.message}`, 'err'); }
  });
  root.querySelector('#backup-keep')?.addEventListener('change', async (event) => {
    try {
      await putSetting('/api/settings/backup', backupPayload(
        root.querySelector('[data-backup-hour]')?.value ?? 3,
        event.target.value,
      ));
      showToast('备份保留份数已保存');
    } catch (e) { showToast(`保存失败：${e.message}`, 'err'); }
  });
  // 立即备份：真的产出一份备份（POST /api/backup/run），成功显示文件名与体积，
  // 失败显示后端给的真实原因（能力未装配 503、落盘失败 500…）。
  // 不看「每日自动备份」开关：手动按钮的意义就是当下就要一份。
  root.querySelector('[data-backup-now]')?.addEventListener('click', async (event) => {
    const btn = event.currentTarget;
    btn.disabled = true;
    try {
      const result = await api('/api/backup/run', {method: 'POST'});
      const name = String(result?.path || '').split(/[\\/]/).pop() || '新备份';
      const size = formatBytes(result?.bytes);
      const line = root.querySelector('[data-backup-result]');
      if (line) {
        line.textContent = `已生成 ${result?.path || name}（${size}）`;
        line.hidden = false;
      }
      showToast(`备份完成：${name}（${size}）`);
    } catch (e) {
      showToast(`备份失败：${e.message}`, 'err');
    } finally { btn.disabled = false; }
  });

  // 保留天数 + 开关持久化（两个天数一起提交，避免互相覆盖）
  // 与备份同理：extra 里**不能**带 enabled。bindToggle 拼体是
  // `{enabled: next, ...extra()}`，extra 在后 —— 带 enabled 就会被页面加载时的旧值
  // 覆盖，开关点下去永远写回旧值（2026-09-15 修的就是这个）。
  // 天数路径不带 enabled 是安全的：write_retention_setting 在 payload 缺该键时
  // 沿用当前值（`bool(payload.get("enabled", current["enabled"]))`），不会把它当关闭。
  // 天数输入：空值/非法值一律**拒绝**，绝不静默回退默认。
  // 原先写的是「输入框空了就用默认天数」：保留天数变小 = 更早删数据 ——
  // 清一下输入框就变成 60，等于悄悄多删 30 天。min/max 属性只是浏览器提示、
  // 不是保护（而且不清空也能直接键入越界值），所以边界在这里按 DOM 上的
  // min/max 显式判一次，并把输入框恢复成上一次真正生效的值。
  const readDays = (input, label, fallback) => {
    const raw = String(input?.value ?? '').trim();
    const value = Number(raw);
    const min = Number(input?.getAttribute('min'));
    const max = Number(input?.getAttribute('max'));
    const inRange = (!Number.isFinite(min) || value >= min)
      && (!Number.isFinite(max) || value <= max);
    if (raw === '' || !Number.isInteger(value) || !inRange) {
      if (input) input.value = String(fallback);
      throw new Error(`${label}需为 ${min}-${max} 之间的整数，已保留原值`);
    }
    return value;
  };
  // 上一次**生效**的天数：校验失败时用它把输入框恢复回去。
  const savedDays = {
    days: Number(retention?.days ?? 60),
    cluster_days: Number(retention?.cluster_days ?? 180),
  };
  const retentionPayload = (scope, saved) => ({
    days: readDays(scope.querySelector('#retention-days'), '原始条目保留天数', saved.days),
    cluster_days: readDays(
      scope.querySelector('#retention-cluster-days'), '结论明细保留天数', saved.cluster_days,
    ),
  });
  const saveRetention = async (message) => {
    try {
      const body = retentionPayload(root, savedDays);
      await putSetting('/api/settings/retention', body);
      Object.assign(savedDays, body); // 提交成功才更新"上一次生效的值"
      showToast(message);
    } catch (e) { showToast(`保存失败：${e.message}`, 'err'); }
  };
  bindToggle(root, '/api/settings/retention', 'retention', (next, r) => retentionPayload(r, savedDays));
  root.querySelector('#retention-days')?.addEventListener('change', () => saveRetention('原始条目保留天数已保存'));
  root.querySelector('#retention-cluster-days')?.addEventListener('change', () => saveRetention('结论明细保留天数已保存'));

  // 学习开关闭环
  bindToggle(root, '/api/settings/learning', 'learning');

  // 到期自动归档（**默认关闭**）：开关与天数必须一起 PUT ——
  // 后端 write_forecast_archive_setting 要求 enabled 必填、days 落在 7–180 内，
  // 分开提交会拿到 400。天数输入非法时用"上一次生效的值"而不是编一个。
  const archiveDaysInput = root.querySelector('#archive-days');
  const archiveDaysValue = () => {
    const raw = Number(archiveDaysInput?.value);
    if (Number.isFinite(raw) && raw >= 7 && raw <= 180) return raw;
    showToast('归档天数需在 7–180 之间，已沿用原值', 'err');
    return Number(archive?.days ?? 30);
  };
  bindToggle(root, '/api/settings/forecast-archive', 'forecast-archive', () => ({
    days: archiveDaysValue(),
  }));
  archiveDaysInput?.addEventListener('change', async () => {
    const toggle = root.querySelector('.toggle[data-key="forecast-archive"]');
    const enabled = toggle?.getAttribute('aria-checked') === 'true';
    try {
      await putSetting('/api/settings/forecast-archive', {
        enabled, days: archiveDaysValue(),
      });
      showToast('设置已保存');
    } catch (e) { showToast(`保存失败：${e.message}`, 'err'); }
  });

  root.querySelector('[data-export]')?.addEventListener('click', async (event) => {
    const btn = event.currentTarget;
    btn.disabled = true;
    try {
      const response = await api('/api/export/mobile-summary', {method: 'POST'});
      const path = root.querySelector('[data-export-path]');
      if (path) { path.textContent = `已导出：${response?.path || '本机 mobile/ 目录'}`; path.hidden = false; }
      showToast('摘要已导出');
    } catch (e) {
      showToast(`导出失败：${e.message}`, 'err');
    } finally { btn.disabled = false; }
  });

  // Agnes AI 快捷预设：填入端点和模型，用户只需填 API 密钥
  root.querySelector('[data-ai-preset-agnes]')?.addEventListener('click', () => {
    const endpointInput = root.querySelector('#ai-endpoint');
    const modelInput = root.querySelector('#ai-model');
    if (endpointInput) endpointInput.value = 'https://apihub.agnes-ai.com/v1/chat/completions';
    if (modelInput) modelInput.value = 'agnes-2.0-flash';
    showToast('已填入 Agnes AI 地址和模型，请输入 API 密钥后保存');
  });

  // 频率选择按钮：点击切换高亮，提交时读取
  let selectedFreq = ai?.frequency || 'medium';
  root.querySelectorAll('[data-freq]').forEach(btn => {
    btn.addEventListener('click', () => {
      selectedFreq = btn.dataset.freq;
      root.querySelectorAll('[data-freq]').forEach(b => {
        b.classList.toggle('btn-primary', b.dataset.freq === selectedFreq);
      });
    });
  });

  const aiStatus = root.querySelector('[data-ai-status]');
  if (aiStatus) aiStatus.textContent = aiStatusText(ai);

  // 远程 AI 启用开关：和旁边三个开关一样点了就落库（端点方法是 POST，不是 PUT）。
  // 提交体只带 {enabled}：AiSettingsService.save() 对缺键沿用当前值，所以
  // endpoint / model / frequency / daily_budget 不会被拖回默认。
  // 点开时后端会校验「模型 + 密钥已配置」，缺失就 400 → 开关回滚 + 真实原因，
  // 而不是像以前那样开关亮着、直到按保存才报错。
  bindToggle(root, '/api/settings/ai', 'ai', undefined, {
    send: postSetting,
    after: async () => {
      const updated = await api('/api/settings/ai'); // 回读，确认 UI 状态保持
      if (aiStatus) aiStatus.textContent = aiStatusText(updated);
    },
  });

  root.querySelector('[data-ai-form]')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const endpoint = event.target.querySelector('#ai-endpoint').value.trim();
    const model = event.target.querySelector('#ai-model').value.trim();
    const token = event.target.querySelector('#ai-key').value.trim();
    const enabled = root.querySelector('[data-ai-enabled]')?.getAttribute('aria-checked') === 'true';
    // 空值绝不能被当成 0 —— 0 的含义是"关闭远程"，静默关掉远程是危险的默认值。
    const budgetRaw = event.target.querySelector('#ai-daily-budget').value.trim();
    const daily_budget = budgetRaw === '' ? Number(ai?.daily_budget ?? 2000) : Number(budgetRaw);
    // 提交体字段名与 remote_ai.AiSettingsService.save() 读取键逐一对齐
    const body = {enabled, endpoint, model, frequency: selectedFreq, daily_budget};
    if (token) body.token = token; // 留空 = 不修改已存密钥（save 仅在有 token 键时覆盖）
    const btn = event.target.querySelector('button[type="submit"]');
    btn.disabled = true;
    try {
      await api('/api/settings/ai', {method: 'POST', body: JSON.stringify(body)});
      const updated = await api('/api/settings/ai'); // 回读，确认 UI 状态保持
      if (aiStatus) aiStatus.textContent = aiStatusText(updated);
      showToast('外部 AI 设置已保存');
      event.target.querySelector('#ai-key').value = '';
    } catch (e) {
      showToast(`保存失败：${e.message}`, 'err');
    } finally {
      btn.disabled = false;
    }
  });

  // 个人利益登记：列表 + 新增
  const CAT_LABELS = {health:'健康安全',cashflow:'现金流',work:'工作收入',policy:'政策权益',family:'家庭关系',assets:'资产负债',opportunity:'机会成长'};
  const interestList = root.querySelector('#interest-list');
  const paintInterests = () => {
    if (!interests) interests = {objects: [], links: []};
    const objs = Array.isArray(interests.objects) ? interests.objects : (interests.objects = []);
    if (!objs.length) { interestList.innerHTML = `<p class="u-dim">暂无登记的利益对象。</p>`; return; }
    interestList.innerHTML = objs.map(o => `<div class="src" data-int="${escapeHtml(o.object_id)}">
      <span class="name">${escapeHtml(o.name || '')}</span>
      <span class="badge">${escapeHtml(CAT_LABELS[o.category] || o.category)}</span>
      <span class="badge">重要度 ${o.importance ?? 3}</span>
      <span class="badge">${escapeHtml(o.privacy_level || 'P2')}</span>
    </div>`).join('');
  };
  root.querySelector('[data-interest-form]')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const name = event.target.querySelector('#int-name').value.trim();
    const category = event.target.querySelector('#int-cat').value;
    const importance = Number(event.target.querySelector('#int-imp').value) || 3;
    const privacy_level = event.target.querySelector('#int-priv').value;
    if (!name) { showToast('名称不能为空', 'err'); return; }
    const btn = event.target.querySelector('button[type="submit"]');
    btn.disabled = true;
    try {
      const obj = await api('/api/interests/objects', {method: 'POST', body: JSON.stringify({name, category, importance, privacy_level})});
      const objs = Array.isArray(interests?.objects) ? interests.objects : (interests.objects = []);
      if (obj?.object_id) objs.unshift(obj);
      event.target.reset();
      paintInterests();
      showToast('已登记个人利益对象');
    } catch (e) { showToast(`登记失败：${e.message}`, 'err'); }
    finally { btn.disabled = false; }
  });
  paintInterests();

  root.querySelector('[data-go-tell]')?.addEventListener('click', () => { location.hash = '#/tell'; });
  root.querySelector('[data-go-today]')?.addEventListener('click', () => { location.hash = '#/today'; });
  root.querySelector('[data-go-calib]')?.addEventListener('click', () => { location.hash = '#/calib'; });

  // 关于：版本号读自本机接口，不发任何网络请求。
  const versionText = root.querySelector('[data-app-version]');
  const updateStatus = root.querySelector('[data-update-status]');
  api('/api/app/version')
    .then((info) => {
      if (versionText) versionText.textContent = `远见 v${info?.version || '未知'}`;
    })
    .catch(() => {
      if (versionText) versionText.textContent = '版本信息读取失败（不影响使用）';
    });

  // 检查更新：全程序唯一会主动外发的操作，只能由用户点击触发，不轮询、不自动下载。
  const updateButton = root.querySelector('[data-check-update]');
  updateButton?.addEventListener('click', async () => {
    updateButton.disabled = true;
    if (updateStatus) updateStatus.textContent = '正在查询…';
    try {
      const result = await api('/api/update-check', {method: 'POST'});
      if (!updateStatus) return;
      if (result?.status !== 'ok') {
        updateStatus.textContent = result?.status === 'no_release'
          ? '还没有发布过可供比较的版本。'
          : '暂时查询不到（可能是网络不通），稍后再试。';
        return;
      }
      if (result.has_update) {
        updateStatus.innerHTML = `有新版本 ${escapeHtml(String(result.latest))}，`
          + `<a href="${escapeHtml(String(result.releases_url))}" target="_blank" rel="noopener">前往下载</a>`;
        return;
      }
      updateStatus.textContent = `已是最新版本（${escapeHtml(String(result.latest))}）。`;
    } catch (e) {
      if (updateStatus) updateStatus.textContent = `查询失败：${e.message}`;
    } finally {
      updateButton.disabled = false;
    }
  });
}
