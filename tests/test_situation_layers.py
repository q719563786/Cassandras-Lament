"""全球态势图层（v8）：GeoJSON 解析 + 独立存储 + 只读接口。

一律离线：夹具内联、`fetcher` 注入、写入 `tempfile` 临时库，**不碰真库、不联网**。
"""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from yuanjian_app.database import Database
from yuanjian_app.external_radar import (
    SITUATION_LAYER_NAMES,
    ExternalRadarService,
)
from yuanjian_app.external_sources import FetchError, parse_geojson
from yuanjian_app.forecasts import ForecastService
from yuanjian_app.http_api import Services, create_server
from yuanjian_app.interests import InterestService
from yuanjian_app.radar_scheduler import RadarScheduler
from yuanjian_app.signals import SignalService


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


def _ms(moment):
    return int(moment.timestamp() * 1000)


RECENT_USGS = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)
OLD_USGS = datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc)

USGS_FIXTURE = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "id": "us0001",
            "properties": {
                "mag": 5.1,
                "place": "12 km SSW of Testville",
                "time": _ms(RECENT_USGS),
                "updated": _ms(RECENT_USGS),
                "title": "M 5.1 - Testville",
                "alert": None,
                "url": "https://example.com/eq/us0001",
            },
            "geometry": {"type": "Point", "coordinates": [100.0, 20.0, 10.0]},
        },
        {
            "type": "Feature",
            "id": "us0004",
            "properties": {
                "mag": 1.0,
                "place": "Old quake",
                "time": _ms(OLD_USGS),
                "title": "M 1.0 - Old",
                "url": "https://example.com/eq/us0004",
            },
            "geometry": {"type": "Point", "coordinates": [10.0, 10.0, 1.0]},
        },
        # 缺 geometry → 跳过
        {
            "type": "Feature",
            "id": "us0002",
            "properties": {"mag": 2.0, "title": "M 2.0 - No Geometry"},
            "geometry": None,
        },
        # 坐标非数值 → 跳过
        {
            "type": "Feature",
            "id": "us0003",
            "properties": {"mag": 3.0, "title": "M 3.0 - Bad Coords"},
            "geometry": {"type": "Point", "coordinates": ["x", 20.0]},
        },
    ],
}

EONET_FIXTURE = {
    "title": "EONET Events",
    "events": [
        {
            "id": "EONET_1",
            "title": "Wildfire Test",
            "description": "7 Miles NE from Nowhere",
            "categories": [{"id": "wildfires", "title": "Wildfires"}],
            "sources": [{"id": "IRWIN", "url": "https://example.com/wf/1"}],
            "geometry": [
                {
                    "date": "2026-09-20T08:00:00Z",
                    "type": "Point",
                    "coordinates": [-98.712896, 26.45826],
                    "magnitudeValue": 500.0,
                }
            ],
        },
        # 无 geometry → 跳过
        {
            "id": "EONET_2",
            "title": "No Geometry Event",
            "categories": [{"id": "wildfires", "title": "Wildfires"}],
        },
    ],
}

GDACS_FIXTURE = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [47.017, -19.34]},
            "properties": {
                "eventtype": "EQ",
                "eventid": 1018431,
                "episodeid": 14,
                "name": "Earthquake Test",
                "description": "Earthquake in Testland",
                "htmldescription": "Orange ...",
                "alertlevel": "Orange",
                "alertscore": 2,
                "fromdate": "2026-09-20T06:00:00",
                "datemodified": "2026-09-20T06:30:00",
                "url": {"report": "https://gdacs.example/1018431"},
            },
        },
        # 无 geometry → 跳过
        {
            "type": "Feature",
            "properties": {
                "eventtype": "TC",
                "eventid": 2,
                "episodeid": 1,
                "name": "No Geo",
            },
        },
    ],
}

FIXTURES = {
    "S-USGS-QUAKE": USGS_FIXTURE,
    "S-NASA-EONET": EONET_FIXTURE,
    "S-GDACS": GDACS_FIXTURE,
}

SITUATION_SOURCE_IDS = tuple(FIXTURES)


class SituationBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "yuanjian.db")
        self.database.initialize()
        self.clock = MutableClock()
        self.service = ExternalRadarService(
            self.database, fetcher=self._fetch, now=self.clock
        )
        self.service.ensure_public_defaults()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _fetch(self, source):
        """离线 fetcher：按 source_id 取夹具，走**真实注册的 config_json** 解析。"""
        fixture = FIXTURES.get(source["source_id"])
        if fixture is None:
            raise FetchError("unsupported", f"测试未提供夹具：{source['source_id']}")
        config = json.loads(source.get("config_json") or "{}")
        return parse_geojson(
            json.dumps(fixture).encode("utf-8"),
            source["source_id"],
            source["name"],
            config,
        )

    def count_situation_rows(self):
        with self.database.connect() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM situation_events"
            ).fetchone()[0]

    def only_situation_sources_enabled(self):
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE external_sources SET enabled = CASE WHEN kind = 'geojson'"
                " THEN 1 ELSE 0 END"
            )

    def refresh_all(self):
        for source_id in SITUATION_SOURCE_IDS:
            self.service.refresh_situation_source(source_id)


class SituationParserTests(SituationBase):
    def test_usgs_fixture_maps_to_earthquake_points_and_skips_dirty_records(self):
        points = self._fetch(self.service._source("S-USGS-QUAKE"))

        self.assertEqual([point.event_key for point in points], ["us0001", "us0004"])
        recent = points[0]
        self.assertEqual(recent.layer, "quake")
        self.assertEqual(recent.title, "M 5.1 - Testville")
        self.assertAlmostEqual(recent.lat, 20.0)
        self.assertAlmostEqual(recent.lon, 100.0)
        self.assertAlmostEqual(recent.magnitude, 5.1)
        # millis → UTC ISO
        self.assertEqual(recent.occurred_at, "2026-09-20T09:00:00Z")

    def test_eonet_fixture_derives_layer_from_category(self):
        points = self._fetch(self.service._source("S-NASA-EONET"))

        self.assertEqual(len(points), 1)
        point = points[0]
        self.assertEqual(point.event_key, "EONET_1")
        self.assertEqual(point.layer, "wildfire")
        self.assertEqual(point.occurred_at, "2026-09-20T08:00:00Z")
        self.assertAlmostEqual(point.lon, -98.712896)
        self.assertAlmostEqual(point.lat, 26.45826)
        self.assertAlmostEqual(point.magnitude, 500.0)
        self.assertEqual(point.url, "https://example.com/wf/1")

    def test_gdacs_fixture_joins_event_and_episode_ids(self):
        points = self._fetch(self.service._source("S-GDACS"))

        self.assertEqual(len(points), 1)
        point = points[0]
        self.assertEqual(point.event_key, "1018431-14")
        self.assertEqual(point.layer, "quake")  # EQ → quake
        self.assertEqual(point.severity, "Orange")
        self.assertEqual(point.url, "https://gdacs.example/1018431")
        self.assertAlmostEqual(point.lat, -19.34)
        self.assertAlmostEqual(point.lon, 47.017)

    def test_empty_and_broken_payloads_are_skipped_not_raised(self):
        config = {"records_path": "features", "layer": "quake",
                  "fields": {"title": "properties.title", "lat": "geometry.coordinates.1",
                             "lon": "geometry.coordinates.0"}}
        self.assertEqual(parse_geojson(b'{"features": []}', "S", "n", config), [])
        # 路径指向非列表
        with self.assertRaises(FetchError):
            parse_geojson(b'{"features": {}}', "S", "n", config)
        # 非法 JSON
        with self.assertRaises(FetchError):
            parse_geojson(b"not json", "S", "n", config)


class SituationStorageTests(SituationBase):
    def test_refresh_writes_points_without_touching_external_items(self):
        self.refresh_all()

        self.assertEqual(self.count_situation_rows(), 4)
        with self.database.connect() as connection:
            external = connection.execute(
                "SELECT COUNT(*) FROM external_items"
            ).fetchone()[0]
            clusters = connection.execute(
                "SELECT COUNT(*) FROM event_clusters"
            ).fetchone()[0]
        self.assertEqual(external, 0)
        self.assertEqual(clusters, 0)

    def test_repeated_refresh_is_idempotent_and_preserves_first_seen(self):
        self.service.refresh_situation_source("S-USGS-QUAKE")
        with self.database.connect() as connection:
            first = connection.execute(
                "SELECT event_id, first_seen_at, last_seen_at FROM situation_events"
                " WHERE event_id=(SELECT event_id FROM situation_events LIMIT 1)"
            ).fetchone()

        self.clock.value += timedelta(minutes=30)
        self.service.refresh_situation_source("S-USGS-QUAKE")

        self.assertEqual(self.count_situation_rows(), 2)  # 未重复追加
        with self.database.connect() as connection:
            second = connection.execute(
                "SELECT first_seen_at, last_seen_at FROM situation_events WHERE event_id=?",
                (first["event_id"],),
            ).fetchone()
        self.assertEqual(second["first_seen_at"], first["first_seen_at"])
        self.assertGreater(second["last_seen_at"], first["last_seen_at"])

    def test_geojson_sources_are_excluded_from_the_external_items_refresh(self):
        """红线：地震数据绝不能经 refresh_due_sources 灌进 external_items。"""
        self.only_situation_sources_enabled()

        due = self.service.refresh_due_sources()

        self.assertEqual(due, 0)
        with self.database.connect() as connection:
            stored = connection.execute(
                "SELECT COUNT(*) FROM external_items"
            ).fetchone()[0]
        self.assertEqual(stored, 0)
        # 走专用路径才写库
        self.assertEqual(self.service.refresh_situation_layers(), 3)
        self.assertEqual(self.count_situation_rows(), 4)

    def test_source_failure_is_recorded_without_losing_cached_points(self):
        self.service.refresh_situation_source("S-GDACS")

        def failing(source):
            raise FetchError("timeout", "外部源请求超时")

        self.service.fetcher = failing
        result = self.service.refresh_situation_source("S-GDACS")

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_type"], "timeout")
        self.assertEqual(self.count_situation_rows(), 1)
        source = self.service._source("S-GDACS")
        self.assertEqual(source["consecutive_failures"], 1)
        self.assertIn("超时", source["last_error"])


class SituationQueryTests(SituationBase):
    def setUp(self):
        super().setUp()
        self.refresh_all()

    def test_layers_report_counts_names_and_fetch_status(self):
        payload = self.service.situation_layers()

        by_layer = {row["layer"]: row for row in payload["layers"]}
        # quake = USGS 两条 + GDACS 的 EQ 一条
        self.assertEqual(by_layer["quake"]["count"], 3)
        self.assertEqual(by_layer["quake"]["name"], "地震")
        self.assertEqual(by_layer["wildfire"]["count"], 1)
        self.assertEqual(
            by_layer["quake"]["latest_occurred_at"], "2026-09-20T09:00:00Z"
        )
        statuses = {row["source_id"]: row["last_status"] for row in payload["sources"]}
        self.assertEqual(statuses["S-USGS-QUAKE"], "ok")

    def test_points_respect_the_default_time_window(self):
        payload = self.service.situation_points()

        self.assertEqual(payload["count"], 3)  # 2 天前那条被 24h 窗口排除
        self.assertEqual(payload["hours"], 24)
        titles = {point["title"] for point in payload["points"]}
        self.assertNotIn("M 1.0 - Old", titles)

    def test_points_filter_by_layer_hours_bbox_and_limit(self):
        wider = self.service.situation_points(hours=72)
        self.assertEqual(wider["count"], 4)

        quakes = self.service.situation_points(layer="quake")
        self.assertEqual(quakes["count"], 2)  # us0001 + GDACS 的 EQ（均在 24h 内）

        boxed = self.service.situation_points(bbox="99,19,101,21")
        self.assertEqual(boxed["count"], 1)
        self.assertEqual(boxed["points"][0]["layer"], "quake")

        limited = self.service.situation_points(limit=2)
        self.assertEqual(limited["count"], 2)

    def test_points_reject_out_of_range_and_malformed_parameters(self):
        for kwargs in (
            {"hours": 0},
            {"hours": 169},
            {"limit": 0},
            {"limit": 2001},
            {"layer": "nope"},
            {"bbox": "1,2,3"},
            {"bbox": "a,b,c,d"},
            {"bbox": "10,10,1,1"},
            {"bbox": "-200,0,10,10"},
        ):
            with self.assertRaises(ValueError):
                self.service.situation_points(**kwargs)

    def test_layer_whitelist_covers_every_registered_layer_name(self):
        self.assertIn("other", SITUATION_LAYER_NAMES)
        for layer in SITUATION_LAYER_NAMES:
            self.service.situation_points(layer=layer)  # 不抛即通过白名单


class SituationHttpContractTests(SituationBase):
    def setUp(self):
        super().setUp()
        self.refresh_all()
        self.token = "situation-token"
        interests = InterestService(self.database)
        services = Services(
            forecasts=ForecastService(self.database),
            interests=interests,
            signals=SignalService(self.database, interests),
            external=self.service,
        )
        self.server = create_server("127.0.0.1", 0, self.token, services)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def get(self, path, token=True):
        request = urllib.request.Request(self.base_url + path)
        if token:
            request.add_header("X-YuanJian-Token", self.token)
        try:
            with self.opener.open(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_layers_endpoint_returns_contract(self):
        status, payload = self.get("/api/situation/layers")

        self.assertEqual(status, 200)
        self.assertIn("layers", payload)
        self.assertIn("sources", payload)
        self.assertTrue(any(row["layer"] == "quake" for row in payload["layers"]))

    def test_points_endpoint_returns_contract(self):
        status, payload = self.get("/api/situation/points?layer=quake&hours=24")

        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 2)  # USGS + GDACS 的 EQ
        point = payload["points"][0]
        self.assertEqual(
            set(point),
            {"event_id", "layer", "layer_name", "title", "lat", "lon",
             "magnitude", "severity", "occurred_at", "source_name", "canonical_url"},
        )
        self.assertTrue(point["event_id"].startswith("G-"))

    def test_boundary_parameters_return_400(self):
        for path in (
            "/api/situation/points?hours=0",
            "/api/situation/points?hours=169",
            "/api/situation/points?limit=0",
            "/api/situation/points?limit=2001",
            "/api/situation/points?layer=nope",
            "/api/situation/points?bbox=1,2,3",
            "/api/situation/points?bbox=9,9,1,1",
            "/api/situation/points?hours=abc",
        ):
            status, payload = self.get(path)
            self.assertEqual(status, 400, path)
            self.assertEqual(payload["error"]["code"], "invalid_request")

    def test_points_require_a_token(self):
        status, _ = self.get("/api/situation/points", token=False)
        self.assertEqual(status, 403)


class SituationSchedulerChainTests(SituationBase):
    """调度链路（2026-09-21 补的测试盲区）。

    覆盖「到期选取 → 调用 fetcher → 落库 → 源状态被更新 → 调度状态可见」这条
    **完整**链路，离线夹具 + 临时库、不联网。

    为什么以前拦不住：`SituationStorageTests` / `SituationHttpContractTests` 测的
    都是 `ExternalRadarService.refresh_situation_layers()` 这一层（服务方法本身
    写得没问题）；而 `RadarScheduler.run_situation_once()` —— 把这个方法接到后台
    循环上的那一环 —— 一条用例都没有，`test_radar_scheduler.py` 里也完全没有
    态势相关断言。于是"任务压根没被调度"这种故障可以一路绿灯溜到安装产物上。
    """

    def scheduler(self, service):
        return RadarScheduler(
            service, database=self.database, poll_seconds=0.01, now=self.clock
        )

    def situation_source_rows(self):
        with self.database.connect() as connection:
            return {
                row["source_id"]: dict(row)
                for row in connection.execute(
                    "SELECT source_id, last_status, last_attempt_at, last_success_at,"
                    " consecutive_failures, next_fetch_at FROM external_sources"
                    " WHERE kind = 'geojson'"
                )
            }

    def task_state(self, name):
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM runtime_state WHERE state_key=?",
                (f"task.{name}",),
            ).fetchone()
        return None if row is None else json.loads(row["value_json"])

    def test_run_situation_once_drives_the_whole_chain(self):
        self.only_situation_sources_enabled()

        payload = self.scheduler(self.service).run_situation_once()

        # ① 到期选取命中了三个源，② fetcher 被调用并按真实 config_json 解析
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["result"], 3)
        # ③ 落库
        self.assertEqual(self.count_situation_rows(), 4)
        # ④ 源状态被更新（现场坏就坏在这里：永远停留在 'never'）
        rows = self.situation_source_rows()
        for source_id in SITUATION_SOURCE_IDS:
            row = rows[source_id]
            self.assertEqual(row["last_status"], "ok", source_id)
            self.assertIsNotNone(row["last_attempt_at"], source_id)
            self.assertIsNotNone(row["last_success_at"], source_id)
            self.assertEqual(row["consecutive_failures"], 0)
            # 下轮到期时间被推到本轮之后，否则每轮都会重复抓
            self.assertGreater(row["next_fetch_at"], "2026-09-20T12:00:00Z", source_id)
        # ⑤ 调度状态可见 —— 没有这条记录，就只能像现场那样靠猜
        state = self.task_state("situation")
        self.assertIsNotNone(state, "task.situation 必须落库")
        self.assertEqual(state["status"], "ok")
        # ⑥ 只读接口能读到刚落库的点（两条 API 路由就是这两个方法）
        self.assertEqual(self.service.situation_points()["count"], 3)
        layers = {row["layer"] for row in self.service.situation_layers()["layers"]}
        self.assertEqual(layers, {"quake", "wildfire"})

    def test_situation_is_serviced_before_the_long_external_walk(self):
        """冷启动：采集一次补抓三十多个源（十几分钟），态势任务不能被它饿死。

        回归现场（真机 + 安装产物）：隔夜重启后所有常规源都过期，采集那一次调用
        要 15~30 分钟才返回；态势块原先写在采集块之后，单线程循环里"采集没返回
        → 态势永远轮不到"，表现为地图整片空白。这里用虚拟 monotonic 把一次采集
        拉长到 30 分钟，钉住"态势先于采集被服务"这条顺序契约。
        """
        import yuanjian_app.radar_scheduler as module

        order = []
        clock = {"now": 100.0}

        class SlowExternalService(ExternalRadarService):
            def refresh_due_sources(self):
                order.append("external")
                clock["now"] += 1800.0  # 一次全量补抓 = 30 分钟
                return 0

        service = SlowExternalService(
            self.database, fetcher=self._fetch, now=self.clock
        )
        service.ensure_public_defaults()
        self.only_situation_sources_enabled()
        real_situation = service.refresh_situation_layers

        def recording_situation():
            order.append("situation")
            return real_situation()

        service.refresh_situation_layers = recording_situation

        class StopAfterFirstWait:
            def __init__(self):
                self.waits = []
                self._set = False

            def is_set(self):
                return self._set

            def set(self):
                self._set = True

            def clear(self):
                self._set = False

            def wait(self, timeout=None):
                self.waits.append(timeout)
                self._set = True
                return True

        scheduler = self.scheduler(service)
        stop = StopAfterFirstWait()
        scheduler._stop = stop

        original = module.time.monotonic
        module.time.monotonic = lambda: clock["now"]
        try:
            scheduler._run()
        finally:
            module.time.monotonic = original

        # 只跑了一轮；这一轮里态势必须先被服务，才不会被 30 分钟的采集挡住
        self.assertEqual(len(stop.waits), 1)
        self.assertTrue(order, "两个任务都该被调到")
        self.assertEqual(order[0], "situation", order)
        self.assertIn("external", order)
        # 并且态势这一趟真的走完了落库 + 源状态更新
        self.assertEqual(self.count_situation_rows(), 4)
        self.assertEqual(
            {row["last_status"] for row in self.situation_source_rows().values()},
            {"ok"},
        )


if __name__ == "__main__":
    unittest.main()
