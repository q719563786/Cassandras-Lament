// 远见 v0.9 · 设置 —— 备份/保留/学习开关持久化 + 移动摘要导出 + 外部 AI 表单
// 总监裁决：情报后台 / Obsidian 入口砍掉，不照抄原型；只留通知中心 + 告诉远见入口
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

// 通用 toggle 行绑定：只绑定 data-key 匹配的开关，避免重复绑定
async function bindToggle(root, path, key, extra) {
  const toggle = root.querySelector(`.toggle[data-key="${key}"]`);
  if (!toggle) return;
  toggle.addEventListener('click', async () => {
    const next = toggle.getAttribute('aria-checked') !== 'true';
    toggle.setAttribute('aria-checked', String(next));
    try {
      const body = {enabled: next, ...(extra ? extra(next, root) : {})};
      await putSetting(path, body);
      showToast('设置已保存');
    } catch (error) {
      toggle.setAttribute('aria-checked', String(!next)); // 失败回滚，不静默
      showToast(`保存失败：${error.message}`, 'err');
    }
  });
}

export async function render(root) {
  // 三个设置端点均在开发中：逐个 catch 降级为"未知"，不阻塞页面
  const [backup, retention, learning] = await Promise.all([
    api('/api/settings/backup').catch(() => null),
    api('/api/settings/retention').catch(() => null),
    api('/api/settings/learning').catch(() => null)
  ]);

  const hours = Array.from({length: 24}, (_, h) => `<option value="${h}"${Number(backup?.hour) === h ? ' selected' : ''}>${String(h).padStart(2, '0')}:00</option>`).join('');

  root.innerHTML = `<div class="u-max">
    <section class="set-sec">
      <h2>自动备份</h2>
      <div class="card">
        ${toggleRow({key: 'backup', name: '每日自动备份', desc: '跨过目标时段后产出 backups/ 新备份，滚动保留 7 份', on: Boolean(backup?.enabled)})}
        <div class="set-row">
          <div><p class="name">目标时段</p><p class="desc">${backup ? '每天在这个时段附近执行一次' : '读取中…未知'}</p></div>
          <div class="u-row">
            <select class="btn btn-sm" data-backup-hour ${backup ? '' : 'disabled'} aria-label="备份目标时段">${hours}</select>
            <button type="button" class="btn btn-sm" data-backup-now ${backup ? '' : 'disabled'}>立即备份</button>
          </div>
        </div>
      </div>
    </section>

    <section class="set-sec">
      <h2>数据保留</h2>
      <div class="card">
        ${toggleRow({key: 'retention', name: '自动清理过期原始条目', desc: '超过保留天数的原始抓取条目将被删除，趋势快照按降采样保留', on: Boolean(retention?.enabled)})}
        <div class="set-row">
          <div><p class="name">保留天数</p><p class="desc">${retention ? `当前 ${retention.days ?? 60} 天` : '读取中…未知'}</p></div>
          <div class="field"><label for="retention-days" class="sr-only">天数</label>
          <input id="retention-days" type="number" min="7" max="365" value="${escapeHtml(String(retention?.days ?? 60))}" ${retention ? '' : 'disabled'}></div>
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
        <form data-ai-form>
          <div class="field u-mb-md"><label for="ai-endpoint">API 地址</label>
          <input id="ai-endpoint" name="endpoint" type="url" placeholder="https://…（留空 = 不启用）"></div>
          <div class="field u-mb-md"><label for="ai-key">API 密钥</label>
          <input id="ai-key" name="api_key" type="password" placeholder="仅保存在本机"></div>
          <div class="u-end"><button type="submit" class="btn btn-primary">保存</button></div>
        </form>
      </div>
    </section>

    <section class="set-sec">
      <h2>其他入口</h2>
      <div class="card">
        <div class="set-row"><p class="name">通知中心</p><button type="button" class="btn btn-sm" data-go-tell-note="notif">从顶栏消息图标打开</button></div>
        <div class="set-row"><p class="name">告诉远见</p><button type="button" class="btn btn-sm" data-go-tell>打开录入视图</button></div>
      </div>
    </section>
  </div>`;

  // 三组开关各自绑定到对应端点（key 过滤，互不串扰）
  bindToggle(root, '/api/settings/backup', 'backup', (next, r) => ({hour: Number(r.querySelector('[data-backup-hour]')?.value || 3)}));
  root.querySelector('[data-backup-hour]')?.addEventListener('change', async (event) => {
    try {
      await putSetting('/api/settings/backup', {enabled: Boolean(backup?.enabled), hour: Number(event.target.value)});
      showToast('目标时段已保存');
    } catch (e) { showToast(`保存失败：${e.message}`, 'err'); }
  });
  root.querySelector('[data-backup-now]')?.addEventListener('click', () => showToast('备份将在下一个目标时段执行'));

  // 保留天数 + 开关持久化
  bindToggle(root, '/api/settings/retention', 'retention', (next, r) => ({days: Number(r.querySelector('#retention-days')?.value || 60)}));
  root.querySelector('#retention-days')?.addEventListener('change', async (event) => {
    try {
      await putSetting('/api/settings/retention', {enabled: Boolean(retention?.enabled), days: Number(event.target.value)});
      showToast('保留天数已保存');
    } catch (e) { showToast(`保存失败：${e.message}`, 'err'); }
  });

  // 学习开关闭环
  bindToggle(root, '/api/settings/learning', 'learning');

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

  root.querySelector('[data-ai-form]')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.target).entries());
    try {
      await api('/api/settings/ai', {method: 'POST', body: JSON.stringify(data)});
      showToast('外部 AI 设置已保存');
      event.target.reset();
    } catch (e) { showToast(`保存失败：${e.message}`, 'err'); }
  });

  root.querySelector('[data-go-tell]')?.addEventListener('click', () => { location.hash = '#/tell'; });
}
