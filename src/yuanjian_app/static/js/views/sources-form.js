// 远见 v0.9 · 源表单 —— 新增 POST /api/external/sources、编辑 PUT /api/external/sources/{id}
import { api, escapeHtml, showToast } from '../api.js';

const KINDS = [
  {value: 'rss', label: 'RSS / Atom'},
  {value: 'html_list', label: '公开网页列表'},
  {value: 'gdelt', label: 'GDELT 全球新闻索引'}
];
const REGIONS = [
  {value: 'heyuan', label: '河源'},
  {value: 'guangdong', label: '广东'},
  {value: 'national', label: '全国'},
  {value: 'global', label: '全球'}
];
const CATEGORIES = [
  {value: 'gov', label: '政务'},
  {value: 'water', label: '水务'},
  {value: 'housing', label: '住建'},
  {value: 'procurement', label: '招标采购'},
  {value: 'news', label: '新闻'},
  {value: 'industry', label: '产业'},
  {value: 'finance', label: '金融'},
  {value: 'general', label: '综合'}
];

const opt = (list, current) => list.map(o =>
  `<option value="${o.value}"${o.value === current ? ' selected' : ''}>${o.label}</option>`).join('');

// 表单 HTML：编辑时用 source 回填 selected/value
export function sourceFormHtml(source) {
  const s = source || {};
  return `<div class="card">
    <h2 class="section-title">${s.id ? '编辑信息源' : '新增信息源'}</h2>
    <form class="form-grid" data-src-form>
      <div class="field"><label for="sf-name">名称</label><input id="sf-name" name="name" required value="${escapeHtml(s.name || '')}" placeholder="例如：河源市水务局公示"></div>
      <div class="field"><label for="sf-url">地址（须公网可访问）</label><input id="sf-url" name="url" type="url" required value="${escapeHtml(s.url || '')}" placeholder="https://…"></div>
      <div class="field"><label for="sf-kind">类型</label><select id="sf-kind" name="kind">${opt(KINDS, s.kind || 'rss')}</select></div>
      <div class="field"><label for="sf-region">区域</label><select id="sf-region" name="region">${opt(REGIONS, s.region || 'heyuan')}</select></div>
      <div class="field"><label for="sf-category">类别</label><select id="sf-category" name="category">${opt(CATEGORIES, s.category || 'general')}</select></div>
      <div class="u-end"><button type="submit" class="btn btn-primary">保存</button><button type="button" class="btn" data-src-cancel>取消</button></div>
    </form>
  </div>`;
}

// 挂载表单：source 为 null 是新增，否则编辑；done 回调刷新列表
export function mountSourceForm(slot, source, done) {
  slot.innerHTML = sourceFormHtml(source);
  const form = slot.querySelector('[data-src-form]');
  slot.querySelector('[data-src-cancel]').addEventListener('click', () => { slot.innerHTML = ''; });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(form).entries());
    try {
      if (source?.id) {
        await api(`/api/external/sources/${encodeURIComponent(source.id)}`, {
          method: 'PUT', body: JSON.stringify(data)
        });
        showToast('源已更新');
      } else {
        await api('/api/external/sources', {method: 'POST', body: JSON.stringify(data)});
        showToast('源已新增');
      }
      slot.innerHTML = '';
      if (done) await done();
    } catch (error) {
      showToast(`保存失败：${error.message}`, 'err');
    }
  });
}
