// 远见 v0.9 · 源管理 —— 源列表 + 健康态 + 区域筛选 + CRUD + OPML 导入（AC-03/AC-04）
import { api, escapeHtml, showToast, withBusy } from '../api.js';
import { yjIcon } from '../icons.js';
import { sourceKindLabel, regionLabel, categoryLabel, sourceHealthLabel, tierLabel } from '../ui_core.js';
import { sourceFormHtml, mountSourceForm } from './sources-form.js';

// 区域筛选：兼容中文（河源/广东…）与枚举（heyuan/guangdong…）双形态
function matchRegion(source, region) {
  if (!region || region === 'all') return true;
  const value = String(source.region || '');
  return value === region || regionLabel(value) === region;
}

function healthDot(source) {
  if (!source.enabled) return 'sdot-off';
  const status = String(source.last_status || '');
  if (status === 'ok') return 'sdot-ok';
  if (status === 'error' || status === 'stale') return 'sdot-warn';
  return 'sdot-warn';
}

function rowHtml(s) {
  const deletable = s.user_managed ? `<button type="button" class="btn btn-sm btn-ghost" data-del="${escapeHtml(s.id)}">删除</button>` : '';
  const tier = s.tier || 'T3';
  const healthText = sourceHealthLabel(s);
  const healthClass = !s.enabled ? 'muted' : (s.last_status === 'ok' ? 'ok' : 'warn');
  return `<div class="src" data-id="${escapeHtml(s.id)}">
    <span class="src-dot ${healthDot(s)}" title="${escapeHtml(healthText)}"></span>
    <div class="src-body">
      <div class="src-head">
        <span class="src-name">${escapeHtml(s.name || s.url || '未命名源')}</span>
        <span class="src-acts">
          <button type="button" class="btn btn-sm btn-ghost" data-toggle="${escapeHtml(s.id)}" data-next="${s.enabled ? 'false' : 'true'}">${s.enabled ? '停用' : '启用'}</button>
          <button type="button" class="btn btn-sm btn-ghost" data-refresh="${escapeHtml(s.id)}">刷新</button>
          <button type="button" class="btn btn-sm btn-ghost" data-edit="${escapeHtml(s.id)}">编辑</button>
          ${deletable}
        </span>
      </div>
      <div class="src-meta">
        <span class="badge badge-neutral">${escapeHtml(sourceKindLabel(s.kind))}</span>
        <span class="badge badge-neutral">${escapeHtml(regionLabel(s.region))}</span>
        <span class="badge badge-neutral">${escapeHtml(categoryLabel(s.category))}</span>
        <span class="badge badge-tier-${tier}">${escapeHtml(tierLabel(tier))}</span>
        <span class="src-health src-health-${healthClass}">${escapeHtml(healthText)}</span>
      </div>
      <div class="src-url" title="${escapeHtml(s.url || '')}">${escapeHtml(s.url || '')}</div>
    </div>
  </div>`;
}

export async function render(root) {
  let sources = [];
  let error = null;
  try {
    const response = await api('/api/external/sources');
    sources = Array.isArray(response?.sources) ? response.sources : [];
  } catch (e) { error = e; }

  let rules = [];
  try {
    const rresp = await api('/api/external/rules');
    rules = Array.isArray(rresp?.rules) ? rresp.rules : [];
  } catch (_) { rules = []; }

  const regions = ['all', 'heyuan', 'guangdong', 'national', 'global'];
  root.innerHTML = `<div class="u-max">
    <div class="u-between u-mb-md">
      <div class="u-row">
        <div class="chips" id="region-chips">
          ${regions.map(r => `<button type="button" class="chip" data-region="${r}" aria-pressed="${r === 'all'}">${r === 'all' ? '全部区域' : regionLabel(r)}</button>`).join('')}
        </div>
        <div class="u-row u-ml-sm">
          <button type="button" class="btn btn-sm btn-secondary" data-bulk="true">批量启用</button>
          <button type="button" class="btn btn-sm btn-secondary" data-bulk="false">批量停用</button>
        </div>
      </div>
      <div class="u-row">
        <label class="btn btn-sm btn-secondary" for="opml-file">导入 OPML</label>
        <input id="opml-file" type="file" accept=".opml,.xml,text/xml" hidden>
        <button type="button" class="btn btn-sm btn-primary" data-new>新增源</button>
      </div>
    </div>
    <section class="card">
      ${error ? `<div class="note">源列表读取失败：${escapeHtml(error.message)}</div>` : ''}
      <div class="src-list" id="src-list"></div>
      <div id="src-empty" hidden class="empty">${yjIcon('ic_offline', 24, '无源')}<p>当前区域没有信息源。</p></div>
    </section>
    <section id="src-form-slot" class="u-mt-md"></section>
    <section class="set-sec">
      <h2>关注词管理</h2>
      <div class="card">
        <form data-rule-form class="u-mb-md">
          <div class="field u-mb-md"><label for="rule-query">关注词</label>
          <input id="rule-query" name="query" type="text" placeholder="如 拆迁 / 水质 / 招标" maxlength="60"></div>
          <div class="field u-mb-md"><label for="rule-importance">重要度（1-5）</label>
          <input id="rule-importance" name="importance" type="number" min="1" max="5" value="3"></div>
          <div class="u-end"><button type="submit" class="btn btn-primary btn-sm">新增关注词</button></div>
        </form>
        <div class="rule-list" id="rule-list"></div>
      </div>
    </section>
  </div>`;

  let filter = 'all';
  const list = root.querySelector('#src-list');
  const emptyBox = root.querySelector('#src-empty');
  const paint = () => {
    const visible = sources.filter(s => matchRegion(s, filter));
    list.innerHTML = visible.map(rowHtml).join('');
    emptyBox.hidden = visible.length > 0;
  };
  paint();

  // 区域 chip 筛选
  root.querySelectorAll('#region-chips .chip').forEach(chip => {
    chip.addEventListener('click', () => {
      filter = chip.dataset.region;
      root.querySelectorAll('#region-chips .chip').forEach(c => c.setAttribute('aria-pressed', String(c === chip)));
      paint();
    });
  });

  // 批量启停：对当前过滤器范围生效（filter='all' 表示全部）。
  root.querySelectorAll('[data-bulk]').forEach(btn => {
    btn.addEventListener('click', () => withBusy(btn, '处理中…', async () => {
      const enabled = btn.dataset.bulk === 'true';
      const scope = filter && filter !== 'all' ? regionLabel(filter) : '全部';
      if (!window.confirm(`${enabled ? '批量启用' : '批量停用'}【${scope}】区域下的所有信息源？`)) return;
      try {
        const response = await api('/api/external/sources/bulk-enabled', {
          method: 'POST',
          body: JSON.stringify({
            enabled,
            region: filter && filter !== 'all' ? filter : '',
          })
        });
        showToast(`${enabled ? '已启用' : '已停用'} ${response?.updated ?? 0} 个信息源`);
        await reload();
      } catch (e) { showToast(`批量操作失败：${e.message}`, 'err'); }
    }));
  });

  // 重新拉取列表
  const reload = async () => {
    try {
      const response = await api('/api/external/sources');
      sources = Array.isArray(response?.sources) ? response.sources : [];
      paint();
    } catch (e) { showToast(`列表刷新失败：${e.message}`, 'err'); }
  };

  // 行内四动作：启停 / 刷新 / 编辑 / 删除（闭包绑定）
  const bindRows = () => {
    list.querySelectorAll('[data-toggle]').forEach(btn => {
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        try {
          await api(`/api/external/sources/${encodeURIComponent(btn.dataset.toggle)}/enabled`, {
            method: 'POST',
            body: JSON.stringify({enabled: btn.dataset.next === 'true'})
          });
          await reload();
        } catch (e) { btn.disabled = false; showToast(`操作失败：${e.message}`, 'err'); }
      });
    });
    list.querySelectorAll('[data-refresh]').forEach(btn => {
      btn.addEventListener('click', () => withBusy(btn, '…', async () => {
        try {
          await api('/api/external/refresh', {method: 'POST', body: JSON.stringify({source_id: btn.dataset.refresh})});
          showToast('已触发抓取');
          await reload();
        } catch (e) { showToast(`刷新失败：${e.message}`, 'err'); }
      }));
    });
    list.querySelectorAll('[data-edit]').forEach(btn => {
      btn.addEventListener('click', () => {
        const source = sources.find(s => String(s.id) === btn.dataset.edit);
        if (source) mountSourceForm(root.querySelector('#src-form-slot'), source, reload);
      });
    });
    list.querySelectorAll('[data-del]').forEach(btn => {
      btn.addEventListener('click', async () => {
        if (!window.confirm('删除这个信息源？历史数据保留，仅停止监听。')) return;
        btn.disabled = true;
        try {
          await api(`/api/external/sources/${encodeURIComponent(btn.dataset.del)}`, {method: 'DELETE'});
          showToast('已删除');
          await reload();
        } catch (e) { btn.disabled = false; showToast(`删除失败：${e.message}`, 'err'); }
      });
    });
  };
  bindRows();
  new MutationObserver(bindRows).observe(list, {childList: true});

  // 新增源表单
  root.querySelector('[data-new]').addEventListener('click', () => {
    mountSourceForm(root.querySelector('#src-form-slot'), null, reload);
  });

  // OPML 导入：file.text() → POST import-opml → 成功/重复/失败计数 toast（AC-04）
  const file = root.querySelector('#opml-file');
  file.addEventListener('change', async () => {
    const picked = file.files && file.files[0];
    if (!picked) return;
    try {
      const xml = await picked.text();
      const response = await api('/api/external/sources/import-opml', {
        method: 'POST',
        body: JSON.stringify({xml})
      });
      showToast(`OPML 导入完成：成功 ${response?.imported ?? 0} / 重复 ${response?.duplicated ?? 0} / 失败 ${response?.failed ?? 0}`);
      await reload();
    } catch (e) {
      showToast(`OPML 导入失败：${e.message}`, 'err');
    } finally {
      file.value = '';
    }
  });

  // 关注词管理：列表 + 启停 + 删除 + 新增
  const ruleList = root.querySelector('#rule-list');
  const paintRules = () => {
    if (!rules.length) {
      ruleList.innerHTML = `<p class="u-dim">暂无关注词。</p>`;
      return;
    }
    ruleList.innerHTML = rules.map(r => `<div class="src rule-row" data-rule="${escapeHtml(r.rule_id)}">
      <div class="src-body">
        <div class="src-head">
          <span class="src-name">${escapeHtml(r.query || '')}</span>
          <span class="src-acts">
            <button type="button" class="btn btn-sm btn-ghost" data-rule-toggle="${escapeHtml(r.rule_id)}" data-next="${r.enabled ? 'false' : 'true'}">${r.enabled ? '停用' : '启用'}</button>
            <button type="button" class="btn btn-sm btn-ghost" data-rule-del="${escapeHtml(r.rule_id)}">删除</button>
          </span>
        </div>
        <div class="src-meta">
          <span class="badge badge-neutral">重要度 ${r.importance ?? 3}</span>
          <span class="badge ${r.enabled ? 'badge-safe' : 'badge-neutral'}">${r.enabled ? '启用中' : '已停用'}</span>
          <span class="src-date">${escapeHtml(String(r.created_at || '').slice(0, 10))}</span>
        </div>
      </div>
    </div>`).join('');
  };
  const bindRules = () => {
    ruleList.querySelectorAll('[data-rule-toggle]').forEach(btn => {
      btn.addEventListener('click', async () => {
        const id = btn.dataset.ruleToggle;
        const enabled = btn.dataset.next === 'true';
        btn.disabled = true;
        try {
          await api(`/api/external/rules/${encodeURIComponent(id)}/enabled`, {method: 'POST', body: JSON.stringify({enabled})});
          const idx = rules.findIndex(r => r.rule_id === id);
          if (idx >= 0) rules[idx].enabled = enabled;
          paintRules();
          bindRules();
        } catch (e) { btn.disabled = false; showToast(`操作失败：${e.message}`, 'err'); }
      });
    });
    ruleList.querySelectorAll('[data-rule-del]').forEach(btn => {
      btn.addEventListener('click', async () => {
        if (!window.confirm('删除这条关注词？历史命中保留。')) return;
        btn.disabled = true;
        try {
          await api(`/api/external/rules/${encodeURIComponent(btn.dataset.ruleDel)}`, {method: 'DELETE'});
          rules = rules.filter(r => r.rule_id !== btn.dataset.ruleDel);
          paintRules();
          bindRules();
          showToast('已删除');
        } catch (e) { btn.disabled = false; showToast(`删除失败：${e.message}`, 'err'); }
      });
    });
  };
  root.querySelector('[data-rule-form]').addEventListener('submit', async (event) => {
    event.preventDefault();
    const query = event.target.querySelector('#rule-query').value.trim();
    const importance = Number(event.target.querySelector('#rule-importance').value) || 3;
    if (!query) { showToast('关注词不能为空', 'err'); return; }
    const btn = event.target.querySelector('button[type="submit"]');
    btn.disabled = true;
    try {
      const resp = await api('/api/external/rules', {method: 'POST', body: JSON.stringify({query, importance})});
      const rule_id = resp?.rule_id;
      if (rule_id) rules.unshift({rule_id, query, importance, enabled: true, created_at: ''});
      event.target.reset();
      paintRules();
      bindRules();
      showToast('已新增关注词');
    } catch (e) { showToast(`新增失败：${e.message}`, 'err'); }
    finally { btn.disabled = false; }
  });
  paintRules();
  bindRules();
}
