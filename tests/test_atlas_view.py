"""「全球态势」地图视图（atlas.js）的**真执行**测试。

为什么必须真执行（照抄 cluster 那轮的教训）：本仓库此前对视图只做"源码里
有没有某段字符串"的文本断言，而那种断言恰恰漏掉过一整页崩掉的缺陷。atlas.js
是本仓库第一个自己拿 canvas 画图的视图 —— 投影算术、命中测试、量级语义、
以及"每个用到的 CSS 类都得有样式"，没有一条是文本断言能兜住的。

本测试做四件事，全部落到真实产物上：

1. **真跑纯函数**：node 里 import 真实的 atlas.js，把项目/反投影/命中测试/
   量级口径/新鲜度/过滤/查询串都跑一遍并断言。
2. **真读底图**：读真实的 `geo/ne_110m_land.geojson`，断言确实是 127 个多边形
   （不是"大概读到了"）。
3. **真跑 render(root)**：最小 DOM 桩 + 一个**记录型 2D 上下文**，
   断言 canvas 上真的画了背景、经纬网、陆块和点（按调用计数对账），
   而不是"函数没抛异常所以大概画了"。
4. **CSS 契约**：atlas.js 里 `class="..."` 用到的每个类都必须在 CSS 里有定义，
   否则页面会静默变成裸 HTML（本项目踩过一次，53 个类没定义）。
"""

import json
import pathlib
import re
import subprocess
import tempfile
import unittest
from urllib.request import pathname2url

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "yuanjian_app" / "static"
ATLAS_JS = STATIC / "js" / "views" / "atlas.js"
LAND_GEOJSON = STATIC / "geo" / "ne_110m_land.geojson"

LAND_FIXTURE = {
    "type": "FeatureCollection",
    "name": "fixture",
    "features": [
        {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[-10, -10], [10, -10], [10, 10], [-10, 10], [-10, -10]]],
            },
        },
        {
            "type": "Feature",
            "geometry": {
                "type": "MultiPolygon",
                "coordinates": [[[[100, 0], [110, 0], [110, 10], [100, 10], [100, 0]]]],
            },
        },
        # 脏数据：没有 geometry → 必须被跳过而不是抛异常
        {"type": "Feature", "properties": {}},
    ],
}

LAYERS_PAYLOAD = {
    "layers": [
        {"layer": "quake", "name": "地震", "count": 2, "latest_occurred_at": "2026-09-20T09:00:00Z",
         "source_ids": ["S-USGS-QUAKE"]},
        {"layer": "storm", "name": "风暴", "count": 0, "latest_occurred_at": "",
         "source_ids": ["S-GDACS"]},
        {"layer": "wildfire", "name": "野火", "count": 1, "latest_occurred_at": "2026-09-20T08:00:00Z",
         "source_ids": ["S-NASA-EONET"]},
    ],
    "sources": [
        {"source_id": "S-USGS-QUAKE", "name": "USGS 地震", "last_status": "ok",
         "last_attempt_at": "2026-09-20T09:00:00Z", "last_success_at": "2026-09-20T09:00:00Z",
         "last_error": "", "consecutive_failures": 0, "next_fetch_at": "2026-09-20T09:20:00Z"},
        {"source_id": "S-NASA-EONET", "name": "NASA EONET", "last_status": "error",
         "last_attempt_at": "2026-09-20T08:00:00Z", "last_success_at": "2026-09-20T06:00:00Z",
         "last_error": "timeout", "consecutive_failures": 2, "next_fetch_at": "2026-09-20T08:30:00Z"},
    ],
}

POINTS_PAYLOAD = {
    "points": [
        {"event_id": "G-1", "layer": "quake", "layer_name": "地震", "title": "M 6.2 - Testville",
         "lat": 20.0, "lon": 100.0, "magnitude": 6.2, "severity": "",
         "occurred_at": "2026-09-20T09:00:00Z", "source_name": "USGS 地震",
         "canonical_url": "https://example.com/eq/1"},
        {"event_id": "G-2", "layer": "wildfire", "layer_name": "野火", "title": "Wildfire Test",
         "lat": 26.0, "lon": -98.0, "magnitude": 500.0, "severity": "Orange",
         "occurred_at": "2026-09-20T08:00:00Z", "source_name": "NASA EONET",
         "canonical_url": ""},
    ],
    "count": 2,
    "hours": 24,
    "limit": 1500,
}

HARNESS = r"""
const fs = require('fs');

const realGeoPath = process.argv[2];
const fixturePath = process.argv[3];
const viewUrl = process.argv[4];

const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));

// ---- 记录型 2D 上下文：靠**调用计数**证明真的画了东西 ----
class RecordingContext {
  constructor() {
    this.calls = { clearRect: 0, fillRect: 0, beginPath: 0, moveTo: 0, lineTo: 0,
                   closePath: 0, fill: 0, stroke: 0, arc: 0, setTransform: 0 };
    this.lineWidth = 0; this.strokeStyle = ''; this.fillStyle = ''; this.globalAlpha = 1;
  }
  setTransform() { this.calls.setTransform += 1; }
  clearRect() { this.calls.clearRect += 1; }
  fillRect() { this.calls.fillRect += 1; }
  beginPath() { this.calls.beginPath += 1; }
  moveTo() { this.calls.moveTo += 1; }
  lineTo() { this.calls.lineTo += 1; }
  closePath() { this.calls.closePath += 1; }
  fill() { this.calls.fill += 1; }
  stroke() { this.calls.stroke += 1; }
  arc() { this.calls.arc += 1; }
  save() {}
  restore() {}
  fillText() {}
}

class FakeElement {
  constructor(tag) {
    this.tag = tag || 'div';
    this.children = [];
    this.attrs = {};
    this.dataset = {};
    this.style = {};
    this.classList = new Set();
    this.innerHTML = '';
    this.textContent = '';
    this.hidden = false;
    this.value = '';
    this.clientWidth = 900;
    this.clientHeight = 460;
  }
  appendChild(c) { this.children.push(c); return c; }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener() { return null; }
  removeEventListener() { return null; }
  querySelector() { return new FakeElement(); }
  querySelectorAll() { return []; }
  getElementsByTagName() { return []; }
  click() { return null; }
  remove() { return null; }
  getBoundingClientRect() { return { left: 0, top: 0, width: 900, height: 460 }; }
}

class FakeCanvas extends FakeElement {
  constructor() {
    super('canvas');
    this.width = 900;
    this.height = 460;
    this._ctx = new RecordingContext();
  }
  getContext(kind) { return kind === '2d' ? this._ctx : null; }
}

class FakeDocument {
  constructor() {
    this.body = new FakeElement('body');
    this.documentElement = {};
    this._byId = {};
    this._bySelector = {};
  }
  createElement(tag) { return new FakeElement(tag); }
  getElementById(id) {
    if (!this._byId[id]) {
      const root = new FakeElement('div');
      root.querySelector = (selector) => this.querySelector(selector);
      this._byId[id] = root;
    }
    return this._byId[id];
  }
  querySelector(selector) {
    if (!this._bySelector[selector]) {
      this._bySelector[selector] = selector.includes('atlas-canvas')
        ? new FakeCanvas()
        : new FakeElement('div');
    }
    return this._bySelector[selector];
  }
  querySelectorAll() { return []; }
  addEventListener() { return null; }
}

const canned = [
  ['/api/situation/layers', JSON.stringify(fixture.layers)],
  ['/api/situation/points', JSON.stringify(fixture.points)],
  ['/geo/ne_110m_land.geojson', JSON.stringify(fixture.land)],
];
function cannedFor(url) {
  for (const [pattern, body] of canned) if (url.includes(pattern)) return body;
  return 'null';
}
class FakeResponse {
  constructor(ok, body) { this.ok = ok; this._body = body; this.status = ok ? 200 : 500; }
  async text() { return this._body; }
}

globalThis.document = new FakeDocument();
globalThis.window = { location: { search: '', pathname: '/', hash: '#/atlas' }, addEventListener() {}, devicePixelRatio: 1 };
globalThis.location = globalThis.window.location;
globalThis.history = { replaceState() {} };
globalThis.navigator = { onLine: true };
globalThis.fetch = async (url) => new FakeResponse(true, cannedFor(String(url)));
globalThis.getComputedStyle = () => ({
  getPropertyValue: (name) => (name === '--red' ? '#D45B6A' : (name === '--blue' ? '#5890C4' : '')),
});
globalThis.requestAnimationFrame = (cb) => setTimeout(cb, 0);
globalThis.cancelAnimationFrame = (h) => clearTimeout(h);

(async () => {
  const out = { ok: false, pure: {}, render: {} };
  let mod;
  try {
    mod = await import(viewUrl);
  } catch (error) {
    out.importError = String((error && error.stack) || error);
    process.stdout.write(JSON.stringify(out));
    return;
  }

  // ---------- 1) 纯函数 ----------
  const pure = out.pure;
  const view = mod.defaultView(900, 460);
  pure.defaultScale = view.scale;
  pure.centre = mod.project(0, 0, view, 900, 460);
  pure.northWest = mod.project(-180, 90, view, 900, 460);
  pure.southEast = mod.project(180, -90, view, 900, 460);
  pure.roundtrip = mod.unproject(123, 45, view, 900, 460);
  pure.reprojected = mod.project(pure.roundtrip.lon, pure.roundtrip.lat, view, 900, 460);
  pure.clampLow = mod.clampScale(0.0000001, view);
  pure.clampHigh = mod.clampScale(100000000, view);
  pure.scaleFloor = view.minScale;
  pure.scaleCeil = view.maxScale;

  pure.units = {
    quake: mod.magnitudeUnit('quake'),
    storm: mod.magnitudeUnit('storm'),
    volcano: mod.magnitudeUnit('volcano'),
    other: mod.magnitudeUnit('other'),
  };
  pure.labels = {
    quake: mod.magnitudeLabel('quake', 6.2, ''),
    storm: mod.magnitudeLabel('storm', 60, ''),
    volcano: mod.magnitudeLabel('volcano', 4, ''),
    severityOnly: mod.magnitudeLabel('wildfire', null, 'Orange'),
    nothing: mod.magnitudeLabel('other', null, ''),
  };
  pure.texts = {
    integer: mod.magnitudeText('quake', 6.0),
    decimal: mod.magnitudeText('quake', 6.24),
    none: mod.magnitudeText('quake', null),
    empty: mod.magnitudeText('quake', ''),
  };
  pure.radii = {
    smallQuake: mod.dotRadius('quake', 2.5, ''),
    bigQuake: mod.dotRadius('quake', 7.5, ''),
    storm: mod.dotRadius('storm', 120, ''),
    volcano: mod.dotRadius('volcano', 5, ''),
    greenFire: mod.dotRadius('wildfire', null, 'Green'),
    redFire: mod.dotRadius('wildfire', null, 'Red'),
  };
  pure.severity = {
    red: mod.severityRank('Red'),
    orange: mod.severityRank('orange'),
    none: mod.severityRank(''),
    other: mod.severityRank('purple'),
  };
  pure.query = {
    plain: mod.buildPointsQuery({ hours: 72, limit: 1500 }),
    layered: mod.buildPointsQuery({ hours: 24, layer: 'quake', limit: 10 }),
  };

  const now = new Date('2026-09-20T12:00:00Z');
  pure.fresh = {
    ok: mod.freshnessLabel({ last_status: 'ok', last_success_at: '2026-09-20T09:00:00Z' }, now),
    error: mod.freshnessLabel({ last_status: 'error', consecutive_failures: 3, last_attempt_at: '2026-09-20T10:00:00Z' }, now),
    never: mod.freshnessLabel(null, now),
    untried: mod.freshnessLabel({ last_status: '' }, now),
  };
  pure.overall = mod.overallFreshness([
    { last_status: 'ok', last_success_at: '2026-09-20T09:00:00Z' },
    { last_status: 'error', last_success_at: '2026-09-20T08:00:00Z' },
  ], now);

  const points = [
    { event_id: 'a', layer: 'quake', lon: 0, lat: 0, magnitude: 5, severity: '' },
    { event_id: 'b', layer: 'wildfire', lon: 10, lat: 0, magnitude: null, severity: 'Red' },
  ];
  pure.filters = {
    all: mod.filterPoints(points, new Set(['quake', 'wildfire'])).length,
    one: mod.filterPoints(points, new Set(['quake'])).map((p) => p.event_id),
    none: mod.filterPoints(points, new Set()).length,
  };
  const atA = mod.project(0, 0, view, 900, 460);
  pure.hit = (mod.hitTest(points, atA.x, atA.y, view, 900, 460) || {}).event_id || null;
  pure.miss = mod.hitTest(points, atA.x + 250, atA.y + 250, view, 900, 460);

  // ---------- 2) 真读底图 ----------
  const realLand = JSON.parse(fs.readFileSync(realGeoPath, 'utf8'));
  pure.land = {
    name: realLand.name,
    features: realLand.features.length,
    polygons: mod.countLandPolygons(realLand.features),
  };

  // 兜底配色读令牌（getComputedStyle 只给了 --red/--blue）
  const palette = mod.readPalette();
  pure.palette = { quake: palette.quake, storm: palette.storm, land: palette.land, bg: palette.bg };

  // 脏数据不抛：fixture 里第三条没有 geometry
  pure.landDrawn = mod.drawLand(new RecordingContext(), fixture.land.features, view, 900, 460, palette);

  // ---------- 3) 真跑 render ----------
  const root = document.getElementById('view-root');
  try {
    await mod.render(root);
    out.ok = true;
  } catch (error) {
    out.renderError = String((error && error.stack) || error);
  }
  const canvas = document.querySelector('.atlas-canvas');
  out.render.calls = canvas._ctx.calls;
  out.render.canvasSize = { width: canvas.width, height: canvas.height };
  const html = (selector) => {
    const node = document.querySelector(selector);
    return node ? String(node.innerHTML) : '';
  };
  out.render.layersHtml = html('[data-role="layers"]');
  out.render.legendHtml = html('[data-role="legend"]');
  out.render.freshHtml = html('[data-role="fresh"]');
  out.render.detailHtml = html('[data-role="detail"]');
  out.render.countText = String(document.querySelector('[data-role="count"]').textContent || '');
  out.render.readoutText = String(document.querySelector('[data-role="readout"]').textContent || '');

  process.stdout.write(JSON.stringify(out));
})().catch((error) => {
  process.stdout.write(JSON.stringify({ ok: false, fatal: String((error && error.stack) || error) }));
});
"""


def _run_harness() -> dict:
    tmp = pathlib.Path(tempfile.mkdtemp())
    harness_path = tmp / "atlas_harness.js"
    harness_path.write_text(HARNESS, encoding="utf-8")
    fixture_path = tmp / "fixture.json"
    fixture_path.write_text(
        json.dumps({"layers": LAYERS_PAYLOAD, "points": POINTS_PAYLOAD, "land": LAND_FIXTURE},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    view_url = "file:" + pathname2url(str(ATLAS_JS))
    result = subprocess.run(
        ["node", str(harness_path), str(LAND_GEOJSON), str(fixture_path), view_url],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=90,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise AssertionError(
            f"node 执行失败 rc={result.returncode}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
    return json.loads(result.stdout)


class AtlasPureFunctionTests(unittest.TestCase):
    """真跑 atlas.js 的纯函数区。"""

    @classmethod
    def setUpClass(cls):
        cls.outcome = _run_harness()
        if cls.outcome.get("importError"):
            raise AssertionError("atlas.js 无法加载：" + cls.outcome["importError"])

    def test_equirectangular_projection_places_the_world_the_right_way_round(self):
        pure = self.outcome["pure"]

        self.assertAlmostEqual(pure["defaultScale"], 2.5)  # 900 / 360
        self.assertAlmostEqual(pure["centre"]["x"], 450.0)
        self.assertAlmostEqual(pure["centre"]["y"], 230.0)
        # 西北角在左上，东南角在右下 —— 纬度方向绝不能画反
        self.assertLess(pure["northWest"]["x"], pure["centre"]["x"])
        self.assertLess(pure["northWest"]["y"], pure["centre"]["y"])
        self.assertGreater(pure["southEast"]["x"], pure["centre"]["x"])
        self.assertGreater(pure["southEast"]["y"], pure["centre"]["y"])
        # 反投影是投影的逆
        self.assertAlmostEqual(pure["reprojected"]["x"], 123.0, places=6)
        self.assertAlmostEqual(pure["reprojected"]["y"], 45.0, places=6)

    def test_zoom_is_clamped_between_fit_and_a_finite_ceiling(self):
        pure = self.outcome["pure"]

        self.assertAlmostEqual(pure["clampLow"], pure["scaleFloor"])
        self.assertAlmostEqual(pure["clampHigh"], pure["scaleCeil"])
        self.assertGreater(pure["scaleCeil"], pure["scaleFloor"])

    def test_magnitude_unit_differs_per_layer_so_values_are_never_compared(self):
        pure = self.outcome["pure"]
        units = pure["units"]

        self.assertIn("震级", units["quake"])
        self.assertIn("风速", units["storm"])
        self.assertIn("VEI", units["volcano"])
        self.assertEqual(len({units["quake"], units["storm"], units["volcano"]}), 3,
                         "三个图层的量纲说明撞车了 —— 界面上就看不出它们不可比")

        self.assertIn("M 6.2", pure["labels"]["quake"])
        self.assertIn("震级", pure["labels"]["quake"])
        self.assertIn("60", pure["labels"]["storm"])
        self.assertIn("风速", pure["labels"]["storm"])
        self.assertIn("VEI 4", pure["labels"]["volcano"])
        # 没有数值时不许编数字，只报预警等级
        self.assertIn("Orange", pure["labels"]["severityOnly"])
        self.assertNotIn("0", pure["labels"]["severityOnly"])
        self.assertEqual(pure["labels"]["nothing"], "未量化")

    def test_magnitude_text_never_invents_a_number(self):
        texts = self.outcome["pure"]["texts"]

        self.assertEqual(texts["integer"], "M 6")
        self.assertEqual(texts["decimal"], "M 6.2")
        self.assertEqual(texts["none"], "")
        self.assertEqual(texts["empty"], "", "空串量级被当成了 0")

    def test_dot_radius_only_scales_within_the_same_unit(self):
        radii = self.outcome["pure"]["radii"]

        self.assertLess(radii["smallQuake"], radii["bigQuake"])
        self.assertLess(radii["greenFire"], radii["volcano"])
        self.assertLess(radii["greenFire"], radii["storm"])
        # 无统一量纲的图层退化为预警等级：等级越高点越大
        self.assertLess(radii["greenFire"], radii["redFire"])

    def test_severity_rank_is_monotonic_and_defaults_to_zero(self):
        severity = self.outcome["pure"]["severity"]

        self.assertEqual(severity["red"], 2)
        self.assertEqual(severity["orange"], 1)
        self.assertEqual(severity["none"], 0)
        self.assertEqual(severity["other"], 0)

    def test_points_query_carries_hours_and_limit(self):
        query = self.outcome["pure"]["query"]

        self.assertIn("hours=72", query["plain"])
        self.assertIn("limit=1500", query["plain"])
        self.assertIn("layer=quake", query["layered"])

    def test_freshness_distinguishes_ok_error_and_never_fetched(self):
        fresh = self.outcome["pure"]["fresh"]
        overall = self.outcome["pure"]["overall"]

        self.assertIn("正常", fresh["ok"])
        self.assertIn("待重试", fresh["error"])
        self.assertIn("×3", fresh["error"])
        self.assertEqual(fresh["never"], "状态未知")
        self.assertIn("尚未抓取", fresh["untried"])
        # 全局新鲜度取最近一次成功，并数出在报错的源
        self.assertIn("今天", overall["label"])
        self.assertEqual(overall["failing"], 1)
        self.assertEqual(overall["total"], 2)

    def test_layer_filter_and_hit_test_agree_on_which_points_are_live(self):
        pure = self.outcome["pure"]

        self.assertEqual(pure["filters"]["all"], 2)
        self.assertEqual(pure["filters"]["one"], ["a"])
        self.assertEqual(pure["filters"]["none"], 0)
        self.assertEqual(pure["hit"], "a")
        self.assertIsNone(pure["miss"])

    def test_real_basemap_really_has_127_polygons(self):
        land = self.outcome["pure"]["land"]

        self.assertEqual(land["name"], "ne_110m_land")
        self.assertEqual(land["features"], 127)
        self.assertEqual(land["polygons"], 127, "陆块数不是 127 —— 底图读坏了")
        # fixture 里三条 feature、其中一条没有 geometry → 只画出两条
        self.assertEqual(self.outcome["pure"]["landDrawn"], 2)

    def test_canvas_palette_is_read_from_theme_tokens(self):
        palette = self.outcome["pure"]["palette"]

        self.assertEqual(palette["quake"], "#D45B6A")  # getComputedStyle 桩给的 --red
        self.assertEqual(palette["storm"], "#5890C4")  # --blue
        self.assertTrue(palette["land"])
        self.assertTrue(palette["bg"])


class AtlasRenderExecutionTests(unittest.TestCase):
    """真 import atlas.js + 真调 render(root) + 记录型 2D 上下文。"""

    @classmethod
    def setUpClass(cls):
        cls.outcome = _run_harness()

    def test_render_completes_without_throwing(self):
        self.assertTrue(
            self.outcome.get("ok"),
            f"render 抛异常：{self.outcome.get('renderError') or self.outcome.get('fatal')}",
        )

    def test_canvas_is_actually_painted_not_just_measured(self):
        calls = self.outcome["render"]["calls"]

        self.assertGreaterEqual(calls["clearRect"], 1, "没有清屏")
        self.assertGreaterEqual(calls["fillRect"], 1, "没有铺底色")
        # 经纬网(11 条经线 + 5 条纬线 + 赤道 + 本初子午线) 至少这些 stroke
        self.assertGreater(calls["stroke"], 10, "经纬网没画出来")
        # 陆块：两条环 → 两次 closePath
        self.assertGreaterEqual(calls["closePath"], 2, "陆地多边形没闭合")
        self.assertGreater(calls["lineTo"], 5, "陆地顶点没连起来")
        # 点：两个点 → 两次 arc
        self.assertEqual(calls["arc"], 2, "事件点没画出来")
        self.assertGreaterEqual(calls["setTransform"], 1, "没有按 devicePixelRatio 校准坐标")

    def test_layers_legend_and_freshness_reach_the_dom(self):
        render = self.outcome["render"]

        self.assertIn("地震", render["layersHtml"])
        self.assertIn("atlas-layer", render["layersHtml"])
        self.assertIn('aria-pressed="true"', render["layersHtml"])
        self.assertIn("2", render["layersHtml"])  # quake 计数

        self.assertIn("不跨图层比较", render["legendHtml"])
        self.assertIn("震级", render["legendHtml"])
        self.assertIn("风速", render["legendHtml"])
        self.assertIn("atlas-swatch", render["legendHtml"])

        self.assertIn("最近成功抓取", render["freshHtml"])
        self.assertIn("待重试", render["freshHtml"])
        self.assertIn("1/2", render["freshHtml"], "报错的源数没显示出来")
        self.assertIn("is-warn", render["freshHtml"])

        self.assertIn("显示 2 / 2 个事件", render["countText"])
        self.assertIn("缩放", render["readoutText"])
        # 还没点选时详情区是引导文案，不是空白
        self.assertIn("点一个事件", render["detailHtml"])


class AtlasCssContractTests(unittest.TestCase):
    """atlas.js 用到的每个类都必须在 CSS 里有定义。"""

    # 由 JS 动态拼出来的类名（不在 class="..." 字面里，或带 ${} 被剥离）
    DYNAMIC_CLASSES = (
        "is-dragging", "is-ok", "is-warn",
        "atlas-layer-quake", "atlas-layer-volcano", "atlas-layer-wildfire",
        "atlas-layer-storm", "atlas-layer-flood", "atlas-layer-drought",
        "atlas-layer-disaster", "atlas-layer-other",
    )

    def test_every_class_used_by_atlas_js_is_defined_in_css(self):
        css = "".join(
            path.read_text(encoding="utf-8")
            for path in sorted((STATIC / "css").rglob("*.css"))
        )
        defined = set(re.findall(r"\.([A-Za-z][\w-]*)", css))

        source = ATLAS_JS.read_text(encoding="utf-8")
        used = set()
        for match in re.finditer(r'class="([^"]*)"', source):
            body = re.sub(r"\$\{[^}]*\}", " ", match.group(1))
            for token in body.split():
                if token and not any(ch in token for ch in ("<>`")):
                    used.add(token)
        used.update(self.DYNAMIC_CLASSES)

        missing = sorted(cls for cls in used if cls not in defined)
        self.assertEqual(missing, [], f"这些类没有样式定义：{missing}")

    def test_atlas_uses_no_inline_style_which_csp_would_block(self):
        """CSP 是 style-src 'self'，内联 style / <style> 会被直接拦掉。

        这条护栏防的是"为了图快写个 style=..." —— 一旦写了，样式在真实浏览器里
        根本不会生效，但 node 测试又看不见。
        """
        source = ATLAS_JS.read_text(encoding="utf-8")

        self.assertNotIn("style=", source)
        self.assertNotIn("<style", source)
        self.assertNotIn(".cssText", source)

    def test_basemap_is_registered_as_a_same_origin_static_asset(self):
        from yuanjian_app import http_api

        entry = http_api.STATIC_FILES.get("/geo/ne_110m_land.geojson")
        self.assertIsNotNone(entry, "底图没登记进 STATIC_FILES，前端会 404")
        self.assertEqual(entry[0], "geo/ne_110m_land.geojson")
        self.assertTrue((STATIC / entry[0]).is_file())

        self.assertIn("/js/views/atlas.js", http_api.STATIC_FILES)

    def test_router_and_nav_expose_the_atlas_view(self):
        router = (STATIC / "js" / "router.js").read_text(encoding="utf-8")
        self.assertIn("atlas", router)
        self.assertIn("全球态势", router)

        shell = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-view="atlas"', shell)
        self.assertIn("全球态势", shell)


if __name__ == "__main__":
    unittest.main()
