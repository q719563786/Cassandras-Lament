# 远见 v0.9 行动引导界面 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把远见改成首次有教程、日常直接给行动建议、行为输入一步可达的三页桌面软件。

**Architecture:** 后端 `CognitionController` 继续负责风险筛选，但补充利益类别、保守建议和中文风险等级；前端纯函数负责最多三条排序、教程状态和输入结果转译，页面渲染只消费这些稳定接口。技术详情与个人资料保留在设置中，默认首页不读取原始新闻接口。

**Tech Stack:** Python 3 标准库、SQLite、原生 HTML/CSS/JavaScript、Node.js 纯函数测试、unittest、pywebview、PyInstaller、PowerShell。

## Global Constraints

- 主导航恰好为“行动首页 / 告诉远见 / 设置”。
- 行动首页最多显示 3 条 L3/L4 个人风险，建议必须先于中文风险级别。
- 公开来源全部失效时必须显示覆盖不足，不得推断没有消息。
- 私人利益、地址、医疗、债务、账户和 Obsidian 原文不得发送给外部 AI。
- 教程状态只保存在本地浏览器存储，不进入数据库或网络。
- 不停止当前正在运行的旧版远见，新版输出到独立 v0.9 目录。

---

### Task 1: 风险建议展示契约

**Files:**
- Modify: `tests/test_cognition.py`
- Modify: `src/yuanjian_app/cognition.py`

**Interfaces:**
- Consumes: `CognitionController.risk_dashboard(source_states=None, limit=3) -> dict`
- Produces: 每个 `items[]` 增加 `interest_category`, `advice`, `reason`, `risk_label`；旧字段暂时保留供详情兼容。

- [ ] **Step 1: Write the failing test**

在风险测试夹具中同时加入现金流与健康利益，断言 `limit` 最大值为 3、L4 返回“高风险”、7 天内 L3 返回“中风险”、30 天 L3 返回“低风险”，并断言含“正式预测账本”的内部建议会替换为类别化保守建议。

```python
dashboard = self.controller.risk_dashboard(healthy_sources, limit=9)
self.assertEqual(len(dashboard["items"]), 3)
self.assertEqual(dashboard["items"][0]["risk_label"], "高风险")
self.assertIn("保留现金", dashboard["items"][0]["advice"])
self.assertNotIn("预测账本", dashboard["items"][0]["advice"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `$env:PYTHONPATH='src'; python -m unittest tests.test_cognition.RiskDashboardTests -v`

Expected: FAIL because `risk_label`, `advice`, and the three-item cap do not exist.

- [ ] **Step 3: Write minimal implementation**

新增 `_plain_advice(category, candidate_action)`，只在建议为空或命中内部术语时按类别回退；查询利益对象的 `category`，并在风险项中返回独立的 `advice`, `reason`, `risk_label`。把 `risk_dashboard` 的限制收紧为 3。

```python
internal_terms = ("正式预测账本", "收集执行证据", "人工确认")
if action and not any(term in action for term in internal_terms):
    return action
return advice_by_category.get(category, advice_by_category["general"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `$env:PYTHONPATH='src'; python -m unittest tests.test_cognition.RiskDashboardTests -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add tests/test_cognition.py src/yuanjian_app/cognition.py
git commit -m "feat: return plain action advice and risk labels"
```

### Task 2: 行动首页与行为输入纯展示规则

**Files:**
- Modify: `tests/test_frontend.py`
- Modify: `src/yuanjian_app/static/risk_ui.js`

**Interfaces:**
- Consumes: `/api/risk-dashboard` items and `/api/events` signal payloads.
- Produces: `visibleRisks(items)`, `inputResult(signal)`, `tutorialSteps()`, `tutorialSeen(storage, version)`, `rememberTutorial(storage, version)`.

- [ ] **Step 1: Write the failing tests**

断言 `visibleRisks` 最多三条；`inputResult` 把 L4/L3/L2/L1 转成中文风险并先返回建议；教程固定三步；存储读写失败时返回安全默认值。

```javascript
assert.deepEqual(ui.visibleRisks(items).map(item => item.cluster_id), ['a','b','watch']);
assert.deepEqual(ui.inputResult({alert_level:'L3', recommended_action:'先保留现金'}), {
  advice:'先保留现金', risk:'中风险'
});
assert.equal(ui.tutorialSteps().length, 3);
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `$env:PYTHONPATH='src'; python -m unittest tests.test_frontend.CognitionFrontendTests -v`

Expected: FAIL because the input and tutorial helpers are missing and the old cap is five.

- [ ] **Step 3: Write minimal implementation**

把排序截断改为 3，新增中文风险映射、输入结果纯函数和三步教程纯函数。存储接口只使用 `getItem`/`setItem` 并捕获异常。

```javascript
function inputResult(signal = {}) {
  return {
    advice: signal.recommended_action || '先记录事实，暂不做不可逆决定。',
    risk: ({L4:'高风险', L3:'中风险', L2:'低风险', L1:'低风险'})[signal.alert_level] || '待判断'
  };
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `$env:PYTHONPATH='src'; python -m unittest tests.test_frontend.CognitionFrontendTests -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add tests/test_frontend.py src/yuanjian_app/static/risk_ui.js
git commit -m "feat: add guided action presentation rules"
```

### Task 3: 三页路由、行动卡与一步输入

**Files:**
- Modify: `tests/test_http_api.py`
- Modify: `src/yuanjian_app/static/index.html`
- Modify: `src/yuanjian_app/static/app.js`
- Modify: `src/yuanjian_app/static/styles.css`

**Interfaces:**
- Consumes: `RiskUI.visibleRisks`, `RiskUI.inputResult`, `/api/events`, `/api/risk-dashboard`.
- Produces: 主路由 `today`, `input`, `system`；`renderInput()`；建议优先的 `riskCard()`。

- [ ] **Step 1: Write the failing HTTP/UI contract tests**

断言首页三项导航的文字和路由，断言 HTML 有教程挂载点，断言脚本存在 `renderInput()` 和“告诉远见并判断”，并断言旧的 `data-view="benefit"` 不再是主入口。

```python
self.assertIn('data-view="input"', body)
self.assertIn("行动首页", body)
self.assertIn("告诉远见", body)
self.assertIn('id="tutorial-root"', body)
self.assertNotIn('data-view="benefit"', body)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `$env:PYTHONPATH='src'; python -m unittest tests.test_http_api.HttpApiTests.test_home_page_is_a_three_entry_risk_cockpit tests.test_http_api.HttpApiTests.test_home_page_makes_personal_risk_the_default_view -v`

Expected: FAIL on new labels, route, and tutorial root.

- [ ] **Step 3: Implement the new navigation and page order**

把 `benefit` 主按钮改为 `input`。首页顶端增加“告诉远见新情况”按钮；卡片用“建议/为什么/最迟/风险”顺序，末尾只留“查看原因”。新增单文本框输入页并保留选填发生时间。

```javascript
async function renderInput() {
  state.view = 'input';
  setHeader('告诉远见', '写发生的事实或你的行为，不需要先分析');
  content.innerHTML = `<form id="event-form" class="panel input-card">...</form>`;
  bindInputForm();
}
```

- [ ] **Step 4: Run focused tests and the whole frontend/API files**

Run: `$env:PYTHONPATH='src'; python -m unittest tests.test_frontend tests.test_http_api -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add tests/test_http_api.py src/yuanjian_app/static/index.html src/yuanjian_app/static/app.js src/yuanjian_app/static/styles.css
git commit -m "feat: make actions and personal input one step away"
```

### Task 4: 首次打开教程与设置重开入口

**Files:**
- Modify: `tests/test_frontend.py`
- Modify: `tests/test_http_api.py`
- Modify: `src/yuanjian_app/static/app.js`
- Modify: `src/yuanjian_app/static/styles.css`

**Interfaces:**
- Consumes: `RiskUI.tutorialSteps()`, `RiskUI.tutorialSeen()`, `RiskUI.rememberTutorial()`.
- Produces: `showTutorial(startIndex=0)`, `closeTutorial()`, 设置子项 `guide`.

- [ ] **Step 1: Write the failing behavior tests**

纯函数测试覆盖首次未读、同版本已读、存储异常；HTTP 脚本契约覆盖 `showTutorial(0)`、教程下一步、跳过和设置重开按钮的可观察标识。

```javascript
const storage = new MapStorage();
assert.equal(ui.tutorialSeen(storage, '0.9'), false);
ui.rememberTutorial(storage, '0.9');
assert.equal(ui.tutorialSeen(storage, '0.9'), true);
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `$env:PYTHONPATH='src'; python -m unittest tests.test_frontend tests.test_http_api -v`

Expected: FAIL because tutorial lifecycle and settings entry do not exist.

- [ ] **Step 3: Implement the tutorial lifecycle**

启动首页渲染后，在未记录 `yuanjian.tutorial.v09` 时打开遮罩。下一步替换教程内容，完成或跳过时写入本地存储并移除遮罩；“重新查看使用教程”忽略已读状态直接打开。

```javascript
const TUTORIAL_VERSION = '0.9';
function maybeShowTutorial() {
  if (!RiskUI.tutorialSeen(localStorage, TUTORIAL_VERSION)) showTutorial(0);
}
```

- [ ] **Step 4: Run frontend and HTTP tests**

Run: `$env:PYTHONPATH='src'; python -m unittest tests.test_frontend tests.test_http_api -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add tests/test_frontend.py tests/test_http_api.py src/yuanjian_app/static/app.js src/yuanjian_app/static/styles.css
git commit -m "feat: teach the three-step YuanJian workflow"
```

### Task 5: 版本、说明与完整发布验收

**Files:**
- Modify: `tests/test_build_config.py`
- Modify: `src/yuanjian_app/__init__.py`
- Modify: `README.md`
- Modify: `使用说明.md`
- Create: `docs/releases/YuanJian-v0.9-verification.md`
- Modify: `00_项目连续性状态.md`（本机外部状态文件，位于仓库之外，不入库）

**Interfaces:**
- Produces: 版本 `0.9.0` 和 `outputs/YuanJianApp-v0.9/YuanJian.exe`。

- [ ] **Step 1: Write the failing version test**

把版本断言改为 `0.9.0`，运行单测确认因生产版本仍为 `0.8.0` 而失败。

Run: `$env:PYTHONPATH='src'; python -m unittest tests.test_build_config.BuildConfigTests.test_package_reports_version_090 -v`

Expected: FAIL with `0.8.0 != 0.9.0`.

- [ ] **Step 2: Update version and user documentation**

将包版本改为 `0.9.0`；说明首页、行为输入、教程和关闭到托盘，保留隐私与覆盖失败边界。

- [ ] **Step 3: Run the full suite and static checks**

Run: `$env:PYTHONPATH='src'; $env:PYTHONDONTWRITEBYTECODE='1'; python -m unittest discover -s tests -v`

Run: `python tools/privacy_scan.py --root .`

Run: `git diff --check`

Expected: all tests pass, privacy scan exits 0, diff check exits 0.

- [ ] **Step 4: Build and smoke-test the separate v0.9 package**

Run: `powershell -ExecutionPolicy Bypass -File build/build_windows.ps1 -OutputDir ../YuanJianApp-v0.9`

Run: `powershell -ExecutionPolicy Bypass -File tools/smoke_packaged.ps1 -ExePath ../YuanJianApp-v0.9/YuanJian.exe`

Expected: build exits 0 and packaged smoke test reports all checks passed.

- [ ] **Step 5: Perform real pywebview acceptance**

启动 v0.9 独立程序，验证首次教程三步、行动首页顺序、告诉远见输入页、设置重开教程、窗口关闭到托盘和安全退出。使用专用临时数据目录，不触碰旧版本进程或正式数据库。

- [ ] **Step 6: Record evidence and commit**

记录测试数量、打包路径、SHA-256、真实窗口验收和已知边界。

```powershell
git add tests/test_build_config.py src/yuanjian_app/__init__.py README.md 使用说明.md docs/releases/YuanJian-v0.9-verification.md
git commit -m "release: verify YuanJian 0.9 guided actions"
```

- [ ] **Step 7: Integrate and synchronize the private repository**

在合并前重新运行全量测试；把功能分支合并回 `main`，再次验证，推送 `origin/main`，并确认 GitHub 仓库可见性仍为 private。只有在这些证据均成立后才更新项目连续性记录。
