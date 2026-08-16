// 远见 v0.9 · 源管理 —— 源列表 + 健康态 + 区域筛选 + CRUD + OPML 导入（AC-03/AC-04）
import { api, escapeHtml, showToast, withBusy } from '../api.js';
import { yjIcon } from '../icons.js';
import { sourceKindLabel, regionLabel, categoryLabel, sourceHealthLabel } from '../ui_core.js';
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
  const deletable = s.user_managed ? `<button type="button" class="btn btn-sm btn-danger" data-del="${escapeHtml(s.id)}">删除</button>` : '';
  return `<div class="src" data-id="${escapeHtml(s.id)}">
    <span class="sdot ${healthDot(s)}" title="${escapeHtml(sourceHealthLabel(s))}"></span>
    <span class="name">${escapeHtml(s.name || s.url || '未命名源')}</span>
    <span class="badge">${escapeHtml(sourceKindLabel(s.kind))}</span>
    <span class="badge">${escapeHtml(regionLabel(s.region))}</span>
    <span class="badge">${escapeHtml(categoryLabel(s.category))}</span>
    <span class="acts">
      <button type="button" class="btn btn-sm" data-toggle="${escapeHtml(s.id)}" data-next="${s.enabled ? 'false' : 'true'}">${s.enabled ? '停用' : '启用'}</button>
      <button type="button" class="btn btn-sm" data-refresh="${escapeHtml(s.id)}">刷新</button>
      <button type="button" class="btn btn-sm" data-edit="${escapeHtml(s.id)}">编辑</button>
      ${deletable}
    </span>
    <span class="url">${escapeHtml(s.url || '')} · ${escapeHtml(sourceHealthLabel(s))}</span>
  </div>`;
}

export async function render(root) {
  let sources = [];
  let error = null;
  try {
    const response = await api('/api/external/sources');
    sources = Array.isArray(response?.sources) ? response.sources : [];
  } catch (e) { error = e; }

  const regions = ['all', 'heyuan', 'guangdong', 'national', 'global'];
  root.innerHTML = `<div class="u-max">
    <div class="u-between u-mb-md">
      <div class="u-row">
        <div class="chips" id="region-chips">
          ${regions.map(r => `<button type="button" class="chip" data-region="${r}" aria-pressed="${r === 'all'}">${r === 'all' ? '全部区域' : regionLabel(r)}</button>`).join('')}
        </div>
        <div class="u-row u-ml-sm">
          <button type="button" class="btn btn-sm" data-bulk="true">批量启用</button>
          <button type="button" class="btn btn-sm" data-bulk="false">批量停用</button>
        </div>
      </div>
      <div class="u-row">
        <label class="btn btn-sm" for="opml-file">导入 OPML</label>
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
}
