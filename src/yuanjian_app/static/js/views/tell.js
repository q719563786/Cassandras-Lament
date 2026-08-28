// 远见 v0.9 · 告诉远见 —— 手动录入视图 + 上次结果卡（today 内联复用 tellBoxHtml/bindTellBox）
import { api, escapeHtml, showToast, withBusy } from '../api.js';
import { inputResult, riskTag, formatLocalTime } from '../ui_core.js';

// 输入框 HTML：scope 参数区分独立视图（tell）与首页内联折叠（inline）
export function tellBoxHtml(scope = 'tell') {
  const prefix = scope === 'inline' ? 'inline-' : '';
  return `<div class="tell">
    <div class="field">
      <label for="${prefix}tell-input">发生了什么事实 / 你做了什么（不需要先分析）</label>
      <textarea id="${prefix}tell-input" rows="3" placeholder="例如：今天接到通知，河源水务项目下月招标。（Ctrl+Enter 快捷提交）"></textarea>
    </div>
    <div class="u-end u-mt-sm">
      <button type="button" class="btn btn-primary btn-sm" data-tell-submit>记录并研判</button>
    </div>
    <div class="tell-result u-mt-sm" hidden></div>
  </div>`;
}

// 绑定输入逻辑：POST /api/events → inputResult 解读 + riskTag
export function bindTellBox(box) {
  const input = box.querySelector('textarea');
  const button = box.querySelector('[data-tell-submit]');
  const result = box.querySelector('.tell-result');
  if (!input || !button || !result) return;

  async function submit() {
    const text = input.value.trim();
    if (!text) {
      showToast('先写点什么再记录', 'err');
      input.focus();
      return;
    }
    await withBusy(button, '正在研判…', async () => {
      try {
        // 契约沿旧版：POST /api/events {text} → {signal:{alert_level, recommended_action}}
        const response = await api('/api/events', {
          method: 'POST',
          body: JSON.stringify({text})
        });
        const signal = response?.signal || (Array.isArray(response?.signals) ? response.signals[0] : null);
        const read = inputResult(signal);
        result.hidden = false;
        result.innerHTML = `<div class="result-box">
          <div class="u-row u-between u-mb-md">${riskTag(signal?.alert_level)}<span class="u-dim">${escapeHtml(formatLocalTime(signal?.updated_at || response?.created_at))}</span></div>
          <p>${escapeHtml(read.advice)}</p>
        </div>`;
        showToast('已记录，远见会持续跟踪');
        input.value = '';
      } catch (error) {
        showToast(`记录失败：${error.message}`, 'err');
      }
    });
  }

  button.addEventListener('click', submit);
  // Ctrl+Enter / Cmd+Enter 快捷提交
  input.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      submit();
    }
  });
}

// 独立视图：输入框 + 上次结果卡（读 /api/signals 真实第一条，无则空态不编造）
export async function render(root) {
  root.innerHTML = `<div class="u-max">
    <section class="card">${tellBoxHtml('tell')}</section>
    <h2 class="section-title u-mt-md">最近一次记录</h2>
    <section id="tell-last" class="card"></section>
  </div>`;
  bindTellBox(root.querySelector('.tell'));
  const last = root.querySelector('#tell-last');
  let signals = null;
  try {
    const response = await api('/api/signals');
    signals = Array.isArray(response?.signals) ? response.signals : [];
  } catch (_) {
    signals = null; // 读不到就明示，不假 0
  }
  if (!signals) {
    last.innerHTML = `<p class="u-dim">暂时读不到历史记录。</p>`;
  } else if (!signals.length) {
    last.innerHTML = `<div class="empty"><p>还没有记录。写下第一条，让远见开始跟踪。</p></div>`;
  } else {
    const item = signals[0];
    const read = inputResult(item);
    last.innerHTML = `<div class="u-row u-between u-mb-md">${riskTag(item.alert_level)}<span class="u-dim">${escapeHtml(formatLocalTime(item.created_at || item.updated_at))}</span></div>
      <p>${escapeHtml(String(item.content || item.summary || '').slice(0, 200))}</p>
      <p class="u-dim u-mt-sm">${escapeHtml(read.advice)}</p>`;
  }
}
