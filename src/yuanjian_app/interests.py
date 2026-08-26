"""Interest-map service used to filter signals by personal relevance."""

import json
from datetime import datetime, timezone
from uuid import uuid4


ALLOWED_CATEGORIES = {"health", "cashflow", "work", "policy", "family", "assets", "opportunity"}
DEFAULT_INTERESTS = (
    ("health", "健康安全"),
    ("cashflow", "现金流"),
    ("work", "工作收入"),
    ("policy", "政策权益"),
    ("family", "家庭关系"),
    ("assets", "资产负债"),
    ("opportunity", "机会成长"),
)


class InterestService:
    """Own generic and user-created interests plus their dependency links."""

    def __init__(self, database):
        self.database = database

    def ensure_defaults(self):
        """Create non-personal filter categories without overwriting local changes."""
        with self.database.connect() as connection:
            for category, name in DEFAULT_INTERESTS:
                connection.execute(
                    "INSERT OR IGNORE INTO interest_objects(object_id, name, category, importance, privacy_level, status) VALUES (?, ?, ?, 3, 'P1', 'active')",
                    (f"I-default-{category}", name, category),
                )

    def list_objects(self):
        with self.database.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT object_id, name, category, importance, privacy_level, status FROM interest_objects ORDER BY importance DESC, name"
                ).fetchall()
            ]

    def list_links(self):
        with self.database.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT link_id, source_id, target_id, relationship, impact_direction, strength FROM interest_links ORDER BY strength DESC, link_id"
                ).fetchall()
            ]

    def create_object(self, data):
        name = " ".join(str(data.get("name", "")).split())
        category = str(data.get("category", "")).strip()
        if not name:
            raise ValueError("利益名称不能为空")
        if category not in ALLOWED_CATEGORIES:
            raise ValueError("利益类别无效")
        try:
            importance = int(data.get("importance", 3))
        except (TypeError, ValueError):
            raise ValueError("重要程度必须是1到5") from None
        if importance not in range(1, 6):
            raise ValueError("重要程度必须是1到5")
        privacy = str(data.get("privacy_level", "P2"))
        if privacy not in {"P1", "P2", "P3"}:
            raise ValueError("隐私级别无效")
        item = {
            "object_id": f"I-{uuid4().hex}",
            "name": name,
            "category": category,
            "importance": importance,
            "privacy_level": privacy,
            "status": "active",
        }
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO interest_objects(object_id, name, category, importance, privacy_level, status) VALUES (:object_id, :name, :category, :importance, :privacy_level, :status)",
                item,
            )
            self._audit(connection, "interest.create", item["object_id"], {"category": category})
        return item

    def create_link(self, data):
        source_id = str(data.get("source_id", ""))
        target_id = str(data.get("target_id", ""))
        if source_id == target_id:
            raise ValueError("利益对象不能连接自身")
        relationship = " ".join(str(data.get("relationship", "")).split())
        if not relationship:
            raise ValueError("关系说明不能为空")
        direction = str(data.get("impact_direction", "mixed"))
        if direction not in {"positive", "negative", "mixed"}:
            raise ValueError("影响方向无效")
        try:
            strength = int(data.get("strength", 3))
        except (TypeError, ValueError):
            raise ValueError("影响强度必须是1到5") from None
        if strength not in range(1, 6):
            raise ValueError("影响强度必须是1到5")
        with self.database.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM interest_objects WHERE object_id IN (?, ?)",
                (source_id, target_id),
            ).fetchone()[0]
            if count != 2:
                raise ValueError("利益对象不存在")
            item = {
                "link_id": f"L-{uuid4().hex}",
                "source_id": source_id,
                "target_id": target_id,
                "relationship": relationship,
                "impact_direction": direction,
                "strength": strength,
            }
            connection.execute(
                "INSERT INTO interest_links(link_id, source_id, target_id, relationship, impact_direction, strength) VALUES (:link_id, :source_id, :target_id, :relationship, :impact_direction, :strength)",
                item,
            )
            self._audit(connection, "interest.link.create", item["link_id"], {"strength": strength})
        return item

    @staticmethod
    def _audit(connection, action, object_id, details):
        connection.execute(
            "INSERT INTO audit_log(occurred_at, action, object_type, object_id, details_json) VALUES (?, ?, 'interest', ?, ?)",
            (datetime.now(timezone.utc).isoformat(), action, object_id, json.dumps(details, ensure_ascii=False)),
        )
