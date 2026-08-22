"""Optional OpenAI Responses adapter and a privacy-safe judgment job queue."""

from __future__ import annotations

import ipaddress
import json
import socket
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from .judgments import (
    ALLOWED_IMPACT_CATEGORIES,
    InvalidJudgmentError,
    JudgmentResult,
    LocalHeuristicProvider,
    MAX_BUNDLE_CHARACTERS,
    MAX_EVIDENCE_SOURCES,
    repair_judgment,
    validate_judgment,
)


DEFAULT_ENDPOINT = "https://api.openai.com/v1/responses"
DAILY_REMOTE_BUDGET = 30


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_endpoint(endpoint):
    parts = urlsplit(str(endpoint))
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("AI地址必须是HTTPS公网地址")
    host = parts.hostname.casefold()
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("AI地址不能指向本机")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and not address.is_global:
        raise ValueError("AI地址不能指向私有网络")
    return str(endpoint)


class RemoteProviderError(RuntimeError):
    def __init__(self, kind, status=None):
        self.kind = str(kind)
        self.status = status
        super().__init__(self.kind)

    @classmethod
    def from_http(cls, status):
        if status in {401, 403}:
            return cls("auth", status)
        if status == 429:
            return cls("rate_limit", status)
        return cls("http_error", status)


def _result_schema():
    string_array = {"type": "array", "items": {"type": "string"}}
    properties = {
        "fact_summary": {"type": "string"},
        "actors": string_array,
        "causal_chain": string_array,
        "uncertainties": string_array,
        "horizons": string_array,
        "probability_low": {"type": "number", "minimum": 0, "maximum": 1},
        "probability_high": {"type": "number", "minimum": 0, "maximum": 1},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "supporting_source_ids": string_array,
        "counter_source_ids": string_array,
        "up_triggers": string_array,
        "down_triggers": string_array,
        "impact_categories": {
            "type": "array",
            "items": {"type": "string", "enum": sorted(ALLOWED_IMPACT_CATEGORIES)},
        },
        # GYW framework (《登高望远》): require the provider to emit the
        # five structured legacy fields so the home page can show real
        # stakeholder / constraint / least-resistance / counter-evidence /
        # leading-indicator analysis instead of UI fallback templates.
        # 稿C v2: four new keys carry structured stakeholder/indicator data —
        # beneficiaries / cost_bearers (with evidence_refs for anti-hallucination),
        # historical_parallel (nullable), observable_signals (array of strings).
        "gyw": {
            "type": "object",
            "properties": {
                "stakeholders": {"type": "string"},
                "constraints": {"type": "string"},
                "least_resistance_path": {"type": "string"},
                "counter_evidence": {"type": "string"},
                "leading_indicators": {"type": "string"},
                "beneficiaries": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "subject": {"type": "string"},
                            "gain": {"type": "string"},
                            "evidence_refs": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["subject", "gain", "evidence_refs"],
                        "additionalProperties": False,
                    },
                },
                "cost_bearers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "subject": {"type": "string"},
                            "cost": {"type": "string"},
                            "evidence_refs": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["subject", "cost", "evidence_refs"],
                        "additionalProperties": False,
                    },
                },
                "historical_parallel": {"type": ["string", "null"]},
                "observable_signals": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "stakeholders",
                "constraints",
                "least_resistance_path",
                "counter_evidence",
                "leading_indicators",
                "beneficiaries",
                "cost_bearers",
                "historical_parallel",
                "observable_signals",
            ],
            "additionalProperties": False,
        },
    }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _default_transport(url, headers, body, timeout):
    host = urlsplit(url).hostname
    try:
        addresses = {
            result[4][0]
            for result in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        }
    except OSError as error:
        raise RemoteProviderError("network") from error
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise RemoteProviderError("unsafe_endpoint")
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(2_000_000)
    except urllib.error.HTTPError as error:
        raise RemoteProviderError.from_http(error.code) from error
    except (urllib.error.URLError, TimeoutError, socket.timeout) as error:
        kind = "timeout" if isinstance(error, (TimeoutError, socket.timeout)) else "network"
        raise RemoteProviderError(kind) from error
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidJudgmentError("远程响应不是有效JSON") from error


class OpenAIResponsesProvider:
    name = "openai_responses"

    def __init__(
        self,
        *,
        model: str,
        token_loader,
        endpoint: str = DEFAULT_ENDPOINT,
        transport=None,
        timeout: int = 45,
    ):
        self.model = str(model or "").strip()
        if not self.model:
            raise ValueError("启用远程AI时必须明确填写模型编号")
        self.endpoint = _validate_endpoint(endpoint)
        self.token_loader = token_loader
        self.transport = transport or _default_transport
        self.timeout = int(timeout)

    def _request_body(self, bundle):
        public = bundle.to_public_dict()
        if len(public["evidence"]) > MAX_EVIDENCE_SOURCES:
            raise ValueError("公开证据包来源超过上限")
        if len(json.dumps(public, ensure_ascii=False)) > MAX_BUNDLE_CHARACTERS:
            raise ValueError("公开证据包字符超过上限")
        system_instruction = public.pop("system_instruction")
        return {
            "model": self.model,
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": system_instruction}],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": json.dumps(public, ensure_ascii=False),
                        }
                    ],
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "yuanjian_judgment",
                    "strict": True,
                    "schema": _result_schema(),
                }
            },
        }

    @staticmethod
    def _output_text(response):
        if isinstance(response, dict) and isinstance(response.get("output_text"), str):
            return response["output_text"]
        for output in response.get("output", ()) if isinstance(response, dict) else ():
            for content in output.get("content", ()) if isinstance(output, dict) else ():
                if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                    return content["text"]
        raise InvalidJudgmentError("远程响应缺少结构化输出文本")

    def analyze(self, bundle) -> JudgmentResult:
        token = str(self.token_loader() or "").strip()
        if not token:
            raise RemoteProviderError("auth")
        body = self._request_body(bundle)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            response = self.transport(self.endpoint, headers, body, self.timeout)
        except RemoteProviderError:
            raise
        except (TimeoutError, socket.timeout) as error:
            raise RemoteProviderError("timeout") from error
        try:
            decoded = json.loads(self._output_text(response))
        except json.JSONDecodeError as error:
            raise InvalidJudgmentError("远程输出不是有效的研判JSON") from error
        try:
            return validate_judgment(decoded, set(bundle.allowed_source_ids))
        except InvalidJudgmentError:
            # 稿D：严格校验失败时尝试宽松修复（缺字段/类型错/数组长度不对），
            # 修复成功则用修复后的远程结果，失败才抛出由调用方降级 local。
            repaired = repair_judgment(decoded, set(bundle.allowed_source_ids))
            if repaired is not None:
                return repaired
            raise


class AiSettingsService:
    """Persist non-secret AI settings while keeping the token in DPAPI storage."""

    STATE_KEY = "ai_settings"

    def __init__(self, database, secret_store):
        self.database = database
        self.secret_store = secret_store

    def _stored(self):
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM runtime_state WHERE state_key=?",
                (self.STATE_KEY,),
            ).fetchone()
        if row is None:
            return {
                "enabled": False,
                "endpoint": DEFAULT_ENDPOINT,
                "model": "",
            }
        try:
            value = json.loads(row["value_json"])
        except json.JSONDecodeError:
            value = {}
        return {
            "enabled": bool(value.get("enabled", False)),
            "endpoint": str(value.get("endpoint") or DEFAULT_ENDPOINT),
            "model": str(value.get("model") or ""),
        }

    def get(self):
        value = self._stored()
        try:
            configured = bool(self.secret_store.load())
        except (OSError, ValueError, RuntimeError):
            configured = False
        return {**value, "configured": configured}

    def save(self, payload):
        current = self._stored()
        enabled = payload.get("enabled", current["enabled"])
        if not isinstance(enabled, bool):
            raise ValueError("AI启用状态无效")
        endpoint = _validate_endpoint(payload.get("endpoint", current["endpoint"]))
        model = str(payload.get("model", current["model"])).strip()
        if "token" in payload:
            self.secret_store.save(str(payload.get("token") or ""))
        configured = bool(self.secret_store.load())
        if enabled and (not model or not configured):
            raise ValueError("启用远程AI前必须填写模型编号和API密钥")
        value = {"enabled": enabled, "endpoint": endpoint, "model": model}
        now = _iso(datetime.now(timezone.utc))
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO runtime_state(state_key,value_json,updated_at)
                VALUES (?,?,?)
                ON CONFLICT(state_key) DO UPDATE SET
                    value_json=excluded.value_json,updated_at=excluded.updated_at
                """,
                (self.STATE_KEY, json.dumps(value, sort_keys=True), now),
            )
        return {**value, "configured": configured}

    def create_remote_provider(self):
        settings = self.get()
        if not settings["enabled"] or not settings["configured"]:
            return None
        return OpenAIResponsesProvider(
            endpoint=settings["endpoint"],
            model=settings["model"],
            token_loader=self.secret_store.load,
        )


class JudgmentQueue:
    def __init__(
        self,
        database,
        *,
        providers,
        bundle_loader,
        local_provider=None,
        now=lambda: datetime.now(timezone.utc),
        daily_budget=DAILY_REMOTE_BUDGET,
    ):
        self.database = database
        self.providers = dict(providers)
        self.bundle_loader = bundle_loader
        self.local_provider = local_provider or LocalHeuristicProvider()
        self.now = now
        self.daily_budget = int(daily_budget)

    def enqueue(self, cluster_id: str, evidence_hash: str, provider: str) -> str:
        if provider not in self.providers:
            raise KeyError(provider)
        created = _iso(self.now())
        job_id = "Q-" + uuid.uuid4().hex
        model = str(getattr(self.providers[provider], "model", "local"))
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO judgment_jobs(
                    job_id,cluster_id,evidence_hash,provider,model,status,
                    attempts,request_chars,created_at,next_attempt_at,last_error
                ) VALUES (?,?,?,?,?,'queued',0,0,?,?,'')
                """,
                (job_id, cluster_id, evidence_hash, provider, model, created, created),
            )
            row = connection.execute(
                """
                SELECT job_id FROM judgment_jobs
                WHERE cluster_id=? AND evidence_hash=? AND provider=?
                """,
                (cluster_id, evidence_hash, provider),
            ).fetchone()
        return row["job_id"]

    def _remote_used_today(self, connection, now):
        start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        return connection.execute(
            """
            SELECT COUNT(*) FROM judgment_jobs
            WHERE status='succeeded' AND provider!='local'
              AND finished_at>=? AND finished_at<?
            """,
            (_iso(start), _iso(end)),
        ).fetchone()[0]

    def remote_used_today(self) -> int:
        """公共接口：今日已成功的远程 AI 判读次数（诊断面板用）。"""
        now = self.now().astimezone(timezone.utc)
        with self.database.connect() as connection:
            return int(self._remote_used_today(connection, now))

    def _persist_judgment(self, connection, job, provider, result, now):
        judgment_id = "J-" + uuid.uuid4().hex
        connection.execute(
            """
            INSERT OR IGNORE INTO judgments(
                judgment_id,cluster_id,provider,evidence_hash,content_json,created_at
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                judgment_id,
                job["cluster_id"],
                provider,
                job["evidence_hash"],
                json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True),
                _iso(now),
            ),
        )
        stored = connection.execute(
            """
            SELECT judgment_id FROM judgments
            WHERE cluster_id=? AND provider=? AND evidence_hash=?
            """,
            (job["cluster_id"], provider, job["evidence_hash"]),
        ).fetchone()["judgment_id"]
        connection.execute(
            """
            UPDATE event_clusters SET latest_judgment_id=?,needs_judgment=0,updated_at=?
            WHERE cluster_id=?
            """,
            (stored, _iso(now), job["cluster_id"]),
        )
        return stored

    def run_due(self, limit: int = 5) -> dict:
        now = self.now().astimezone(timezone.utc)
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM judgment_jobs
                WHERE status IN ('queued','retry','queued_budget')
                  AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                ORDER BY created_at,job_id LIMIT ?
                """,
                (_iso(now), max(1, min(int(limit), 100))),
            ).fetchall()
            used = self._remote_used_today(connection, now)
        summary = {"succeeded": 0, "deferred": 0, "failed": 0}
        for row in rows:
            job = dict(row)
            provider = self.providers.get(job["provider"])
            if provider is None:
                continue
            if job["provider"] != "local" and used >= self.daily_budget:
                tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
                with self.database.connect() as connection:
                    connection.execute(
                        "UPDATE judgment_jobs SET status='queued_budget',next_attempt_at=?,last_error='daily_budget' WHERE job_id=?",
                        (_iso(tomorrow), job["job_id"]),
                    )
                summary["deferred"] += 1
                continue
            bundle = self.bundle_loader(job["cluster_id"])
            request_chars = len(json.dumps(bundle.to_public_dict(), ensure_ascii=False))
            try:
                result = provider.analyze(bundle)
                with self.database.connect() as connection:
                    self._persist_judgment(connection, job, job["provider"], result, now)
                    connection.execute(
                        """
                        UPDATE judgment_jobs SET status='succeeded',attempts=attempts+1,
                            request_chars=?,finished_at=?,last_error=''
                        WHERE job_id=?
                        """,
                        (request_chars, _iso(now), job["job_id"]),
                    )
                used += int(job["provider"] != "local")
                summary["succeeded"] += 1
            except InvalidJudgmentError:
                fallback = self.local_provider.analyze(bundle)
                with self.database.connect() as connection:
                    self._persist_judgment(connection, job, "local", fallback, now)
                    connection.execute(
                        """
                        UPDATE judgment_jobs SET status='invalid_output',attempts=attempts+1,
                            request_chars=?,finished_at=?,last_error='invalid_output'
                        WHERE job_id=?
                        """,
                        (request_chars, _iso(now), job["job_id"]),
                    )
                summary["failed"] += 1
            except RemoteProviderError as error:
                attempts = int(job["attempts"]) + 1
                if error.kind == "auth":
                    status = "paused_auth"
                    next_attempt = _iso(now)
                else:
                    status = "retry"
                    delay = min(60, 15 * (2 ** (attempts - 1)))
                    next_attempt = _iso(now + timedelta(minutes=delay))
                with self.database.connect() as connection:
                    connection.execute(
                        """
                        UPDATE judgment_jobs SET status=?,attempts=?,request_chars=?,
                            next_attempt_at=?,last_error=? WHERE job_id=?
                        """,
                        (
                            status,
                            attempts,
                            request_chars,
                            next_attempt,
                            error.kind,
                            job["job_id"],
                        ),
                    )
                summary["failed"] += 1
        return summary
