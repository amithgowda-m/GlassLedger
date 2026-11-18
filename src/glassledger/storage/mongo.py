from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional
from bson.decimal128 import Decimal128
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.database import Database

from glassledger.config import settings
from glassledger.domain.models import DecisionSnapshot, StrategyConfig, Tick


def _to_bson(val: Any) -> Any:
    """Recursively converts Decimals to Decimal128 for MongoDB storage."""
    if isinstance(val, Decimal):
        return Decimal128(val)
    if isinstance(val, dict):
        return {k: _to_bson(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_to_bson(v) for v in val]
    return val


def _from_bson(val: Any) -> Any:
    """Recursively converts Decimal128 back to Decimal."""
    if isinstance(val, Decimal128):
        return val.to_decimal()
    if isinstance(val, dict):
        return {k: _from_bson(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_from_bson(v) for v in val]
    return val


class MongoStorage:
    def __init__(self, uri: Optional[str] = None, db_name: Optional[str] = None):
        self.uri = uri or settings.mongo_uri
        self.db_name = db_name or settings.mongo_db_name
        self._client: Optional[MongoClient] = None
        self._db: Optional[Database] = None

    def connect(self) -> None:
        if self._client is None:
            self._client = MongoClient(self.uri, serverSelectionTimeoutMS=5000)
            self._db = self._client[self.db_name]

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
            self._db = None

    @property
    def client(self) -> MongoClient:
        if self._client is None:
            self.connect()
        assert self._client is not None
        return self._client

    @property
    def db(self) -> Database:
        if self._db is None:
            self.connect()
        assert self._db is not None
        return self._db

    def run_migrations(self) -> None:
        """Initializes collections, time-series, and indexes."""
        db = self.db
        existing = db.list_collection_names()

        # 1. Price history (time-series)
        if "price_history" not in existing:
            try:
                db.create_collection(
                    "price_history",
                    timeseries={
                        "timeField": "ts",
                        "metaField": "symbol",
                        "granularity": "seconds",
                    },
                )
            except Exception:
                pass
        db.price_history.create_index([("symbol", ASCENDING), ("ts", DESCENDING)])
        db.price_history.create_index([("dataset_name", ASCENDING), ("ts", ASCENDING)])

        # 2. Strategies
        if "strategies" not in existing:
            try:
                db.create_collection(
                    "strategies",
                    validator={
                        "$jsonSchema": {
                            "bsonType": "object",
                            "required": [
                                "strategy_id",
                                "investor_id",
                                "strategy_type",
                                "parameters",
                                "version",
                                "is_active",
                            ],
                            "properties": {
                                "strategy_id": {"bsonType": "string"},
                                "investor_id": {"bsonType": ["int", "long"]},
                                "strategy_type": {
                                    "enum": ["MA_CROSSOVER", "RSI_THRESHOLD"]
                                },
                                "version": {"bsonType": "string"},
                                "is_active": {"bsonType": "bool"},
                                "parameters": {"bsonType": "object"},
                            },
                        }
                    },
                )
            except Exception:
                pass
        db.strategies.create_index([("strategy_id", ASCENDING)], unique=True)
        db.strategies.create_index([("is_active", ASCENDING), ("strategy_type", ASCENDING)])

        # 3. Strategy execution log
        if "strategy_execution_log" not in existing:
            try:
                db.create_collection(
                    "strategy_execution_log",
                    validator={
                        "$jsonSchema": {
                            "bsonType": "object",
                            "required": [
                                "decision_id",
                                "strategy_id",
                                "evaluation_seq",
                                "trigger_tick",
                                "indicator_state",
                                "signal",
                            ],
                            "properties": {
                                "decision_id": {"bsonType": "string"},
                                "strategy_id": {"bsonType": "string"},
                                "evaluation_seq": {"bsonType": ["long", "int"]},
                                "trigger_tick": {"bsonType": "object"},
                                "indicator_state": {"bsonType": "object"},
                                "signal": {"enum": ["BUY", "SELL", "HOLD"]},
                                "order_id": {"bsonType": ["string", "null"]},
                            },
                        }
                    },
                )
            except Exception:
                pass
        db.strategy_execution_log.create_index([("decision_id", ASCENDING)], unique=True)
        db.strategy_execution_log.create_index(
            [("strategy_id", ASCENDING), ("evaluation_seq", ASCENDING)], unique=True
        )

    def save_ticks(self, ticks: list[Tick], dataset_name: str = "default") -> int:
        if not ticks:
            return 0
        docs = []
        for t in ticks:
            docs.append(
                {
                    "ts": t.timestamp if t.timestamp.tzinfo else t.timestamp.replace(tzinfo=timezone.utc),
                    "symbol": t.symbol,
                    "event_id": t.event_id,
                    "price": Decimal128(t.price),
                    "volume": t.volume,
                    "dataset_name": dataset_name,
                }
            )
        res = self.db.price_history.insert_many(docs)
        return len(res.inserted_ids)

    def get_ticks(self, dataset_name: str = "default") -> list[Tick]:
        cursor = self.db.price_history.find({"dataset_name": dataset_name}).sort("ts", ASCENDING)
        ticks = []
        for doc in cursor:
            price_dec = doc["price"].to_decimal() if isinstance(doc["price"], Decimal128) else Decimal(str(doc["price"]))
            ticks.append(
                Tick(
                    event_id=doc["event_id"],
                    symbol=doc["symbol"],
                    price=price_dec,
                    volume=doc["volume"],
                    timestamp=doc["ts"],
                )
            )
        return ticks

    def log_decision(self, decision: DecisionSnapshot) -> str:
        doc = {
            "decision_id": decision.decision_id,
            "strategy_id": decision.strategy_id,
            "evaluation_seq": int(decision.evaluation_seq),
            "trigger_tick": _to_bson(decision.trigger_tick),
            "indicator_state": _to_bson(decision.indicator_state),
            "signal": decision.signal,
            "order_id": decision.order_id,
            "created_at": datetime.now(timezone.utc),
        }
        self.db.strategy_execution_log.replace_one(
            {"decision_id": decision.decision_id}, doc, upsert=True
        )
        return decision.decision_id

    def get_decision(self, decision_id: str) -> Optional[dict[str, Any]]:
        doc = self.db.strategy_execution_log.find_one({"decision_id": decision_id}, {"_id": 0})
        return _from_bson(doc) if doc else None

    def get_decisions_for_strategy(self, strategy_id: str) -> list[dict[str, Any]]:
        cursor = self.db.strategy_execution_log.find(
            {"strategy_id": strategy_id}, {"_id": 0}
        ).sort("evaluation_seq", ASCENDING)
        return [_from_bson(doc) for doc in cursor]

    def save_strategy(self, config: StrategyConfig) -> None:
        doc = {
            "strategy_id": config.strategy_id,
            "investor_id": config.investor_id,
            "strategy_type": config.strategy_type,
            "version": config.version,
            "is_active": config.is_active,
            "parameters": _to_bson(config.parameters),
            "updated_at": datetime.now(timezone.utc),
        }
        self.db.strategies.replace_one({"strategy_id": config.strategy_id}, doc, upsert=True)

    def get_strategy(self, strategy_id: str) -> Optional[dict[str, Any]]:
        doc = self.db.strategies.find_one({"strategy_id": strategy_id}, {"_id": 0})
        return _from_bson(doc) if doc else None
