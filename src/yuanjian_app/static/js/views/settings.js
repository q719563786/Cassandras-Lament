// 远见 v1.1 · 设置 —— 备份/保留/学习开关持久化 + 移动摘要导出 + 外部 AI 表单
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

  // 远程 AI 当前状态文案（端点域名 + 模型 + 启用/密钥状态 + 频率）
function aiStatusText(ai) {
  if (!ai) return '读取中…未知';
  const domain = (ai.endpoint || '').replace(/^https?:\/\//, '').split('/')[0] || '—';
  const state = ai.enabled ? '已启用' : '未启用';
  const key = ai.configured ? '密钥已配置' : '密钥未配置';
  const freqMap = {low: '低(每日21点)', medium: '中(每6小时)', high: '高(每小时)'};
  const freq = freqMap[ai.frequency] || '中(每6小时)';
  return `${state} · 模型 ${ai.model || '—'} · 端点 ${domain} · ${key} · 频率 ${freq}`;
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
  const [backup, retention, learning, ai, interests] = await Promise.all([
    api('/api/settings/backup').catch(() => null),
    api('/api/settings/retention').catch(() => null),
    api('/api/settings/learning').catch(() => null),
    api('/api/settings/ai').catch(() => null),
    api('/api/interests').catch(() => null)
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
        <div class="set-row">
          <div><p class="name">启用远程 AI</p><p class="desc">开启后远见用你填的模型做外部研判，密钥只存本机</p></div>
          <button type="button" class="toggle" role="switch" data-ai-enabled
            aria-checked="${ai?.enabled ? 'true' : 'false'}" aria-label="启用远程AI"></button>
        </div>
        <div class="set-row">
          <div><p class="name">分析频率</p><p class="desc">低=每天21点汇总一次 · 中=每6小时 · 高=每小时</p></div>
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

  // 远程 AI 启用开关：仅切换 aria-checked，提交时读取
  const aiToggle = root.querySelector('[data-ai-enabled]');
  aiToggle?.addEventListener('click', () => {
    const next = aiToggle.getAttribute('aria-checked') !== 'true';
    aiToggle.setAttribute('aria-checked', String(next));
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

  root.querySelector('[data-ai-form]')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const endpoint = event.target.querySelector('#ai-endpoint').value.trim();
    const model = event.target.querySelector('#ai-model').value.trim();
    const token = event.target.querySelector('#ai-key').value.trim();
    const enabled = root.querySelector('[data-ai-enabled]')?.getAttribute('aria-checked') === 'true';
    // 提交体字段名与 remote_ai.AiSettingsService.save() 读取键逐一对齐
    const body = {enabled, endpoint, model, frequency: selectedFreq};
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
}
