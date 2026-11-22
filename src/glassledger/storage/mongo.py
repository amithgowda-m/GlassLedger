from datetime import datetime, timezone
from decimal import Decimal
import logging
from pathlib import Path
import pickle
import re
import sqlite3
from typing import Any, Optional
import uuid

from bson.decimal128 import Decimal128
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.database import Database

from glassledger.config import settings
from glassledger.domain.models import DecisionSnapshot, StrategyConfig, Tick


log = logging.getLogger("glassledger.storage.mongo")
_FALLBACK_WARNED = False


def _warn_fallback_once(path: Path) -> None:
    global _FALLBACK_WARNED
    if _FALLBACK_WARNED:
        return
    _FALLBACK_WARNED = True
    log.warning(
        "GLASSLEDGER TELEMETRY IS RUNNING ON THE NON-AUTHORITATIVE SQLITE FALLBACK (%s): "
        "MongoDB is unreachable; documents are pickled into SQLite with no $jsonSchema validation "
        "and no unique indexes. Use it for demos only.",
        path,
    )


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


class FallbackCursor:
    def __init__(self, docs: list[dict]):
        self._docs = list(docs)

    def sort(self, key_or_list, direction=1) -> "FallbackCursor":
        if isinstance(key_or_list, list):
            for k, d in reversed(key_or_list):
                rev = (d == -1 or d == "desc" or str(d).lower() == "descending")
                self._docs.sort(key=lambda x: (x.get(k) is None, x.get(k)), reverse=rev)
        else:
            rev = (direction == -1 or direction == "desc" or str(direction).lower() == "descending")
            self._docs.sort(key=lambda x: (x.get(key_or_list) is None, x.get(key_or_list)), reverse=rev)
        return self

    def limit(self, n: int) -> "FallbackCursor":
        self._docs = self._docs[:n]
        return self

    def __iter__(self):
        return iter(self._docs)

    def __len__(self) -> int:
        return len(self._docs)


class FallbackCollection:
    def __init__(self, db_path: str, name: str):
        self.db_path = db_path
        self.name = name
        self._init_table()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        return conn

    def _init_table(self) -> None:
        with self._get_conn() as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS coll_{self.name} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_id TEXT UNIQUE,
                    doc BLOB
                );
                """
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{self.name}_doc_id ON coll_{self.name}(doc_id);"
            )

    def insert_one(self, doc: dict) -> Any:
        d = dict(doc)
        doc_id = str(d.get("decision_id") or d.get("strategy_id") or d.get("event_id") or uuid.uuid4())
        data = pickle.dumps(d)
        with self._get_conn() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO coll_{self.name} (doc_id, doc) VALUES (?, ?)",
                (doc_id, data),
            )
        return type("InsertOneResult", (), {"inserted_id": doc_id})()

    def insert_many(self, docs: list[dict]) -> Any:
        rows = []
        for doc in docs:
            d = dict(doc)
            doc_id = str(d.get("decision_id") or d.get("strategy_id") or d.get("event_id") or uuid.uuid4())
            rows.append((doc_id, pickle.dumps(d)))
        with self._get_conn() as conn:
            conn.executemany(
                f"INSERT OR REPLACE INTO coll_{self.name} (doc_id, doc) VALUES (?, ?)", rows
            )
        return type("InsertManyResult", (), {"inserted_ids": [r[0] for r in rows]})()

    def _match(self, doc: dict, filt: Optional[dict]) -> bool:
        if not filt:
            return True
        for k, v in filt.items():
            val = doc.get(k)
            if isinstance(v, dict):
                if "$regex" in v:
                    if val is None or not re.search(v["$regex"], str(val)):
                        return False
                elif "$eq" in v:
                    if val != v["$eq"]:
                        return False
                elif "$in" in v:
                    if val not in v["$in"]:
                        return False
                elif "$gt" in v:
                    if val is None or val <= v["$gt"]:
                        return False
                elif "$lt" in v:
                    if val is None or val >= v["$lt"]:
                        return False
            else:
                if val != v:
                    return False
        return True

    def find(self, filter: Optional[dict] = None, projection: Optional[dict] = None) -> FallbackCursor:
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute(f"SELECT doc FROM coll_{self.name}")
            docs = []
            for (blob,) in cur.fetchall():
                doc = pickle.loads(blob)
                if self._match(doc, filter):
                    if projection and projection.get("_id") == 0:
                        doc.pop("_id", None)
                    docs.append(doc)
            return FallbackCursor(docs)

    def find_one(self, filter: Optional[dict] = None, projection: Optional[dict] = None) -> Optional[dict]:
        cursor = self.find(filter, projection)
        for doc in cursor:
            return doc
        return None

    def count_documents(self, filter: Optional[dict] = None) -> int:
        if not filter:
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute(f"SELECT COUNT(*) FROM coll_{self.name}")
                return cur.fetchone()[0]
        return len(self.find(filter))

    def replace_one(self, filter: dict, doc: dict, upsert: bool = False) -> None:
        d = dict(doc)
        doc_id = str(
            d.get("decision_id")
            or d.get("strategy_id")
            or d.get("event_id")
            or (list(filter.values())[0] if filter else uuid.uuid4())
        )
        data = pickle.dumps(d)
        with self._get_conn() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO coll_{self.name} (doc_id, doc) VALUES (?, ?)",
                (doc_id, data),
            )

    def update_one(self, filter: dict, update: dict, upsert: bool = False) -> None:
        existing = self.find_one(filter)
        if existing:
            if "$set" in update:
                existing.update(update["$set"])
            self.replace_one(filter, existing)
        elif upsert:
            new_doc = dict(filter)
            if "$set" in update:
                new_doc.update(update["$set"])
            self.replace_one(filter, new_doc)

    def create_index(self, keys, **kwargs) -> None:
        pass


class FallbackAdmin:
    def command(self, cmd: str, *args, **kwargs) -> dict[str, Any]:
        return {"version": "8.0.0 (SQLite Fallback)", "ok": 1.0}


class FallbackDatabase:
    def __init__(self, db_path: str, name: str):
        self._db_path = db_path
        self.name = name
        self._collections: dict[str, FallbackCollection] = {}

    def list_collection_names(self) -> list[str]:
        return ["price_history", "strategies", "strategy_execution_log"]

    def create_collection(self, name: str, **kwargs) -> FallbackCollection:
        return self[name]

    def __getitem__(self, name: str) -> FallbackCollection:
        if name not in self._collections:
            self._collections[name] = FallbackCollection(self._db_path, name)
        return self._collections[name]

    def __getattr__(self, name: str) -> FallbackCollection:
        return self[name]


class FallbackMongoClient:
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._dbs: dict[str, FallbackDatabase] = {}
        self.admin = FallbackAdmin()

    def __getitem__(self, name: str) -> FallbackDatabase:
        if name not in self._dbs:
            self._dbs[name] = FallbackDatabase(self._db_path, name)
        return self._dbs[name]

    def __getattr__(self, name: str) -> FallbackDatabase:
        return self[name]

    def close(self) -> None:
        pass


class MongoStorage:
    def __init__(self, uri: Optional[str] = None, db_name: Optional[str] = None):
        self.uri = uri or settings.mongo_uri
        self.db_name = db_name or settings.mongo_db_name
        self._client: Optional[Any] = None
        self._db: Optional[Any] = None
        self._probed: Optional[bool] = None
        self._sqlite_path = Path(settings.sqlite_data_dir) / "glassledger_telemetry.db"

    @property
    def is_sqlite_fallback(self) -> bool:
        return not self._probe_mongo()

    def _probe_mongo(self) -> bool:
        """Instantaneous TCP socket check for MongoDB port."""
        if self._probed is not None:
            return self._probed
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.15)
                host = "127.0.0.1"
                port = 27017
                if "://" in self.uri:
                    part = self.uri.split("://")[1].split("/")[0]
                    if ":" in part:
                        host, port_str = part.split(":")
                        port = int(port_str)
                    else:
                        host = part
                res = s.connect_ex((host, port))
                self._probed = (res == 0)
        except Exception:
            self._probed = False
        return self._probed

    def connect(self) -> None:
        if self._probe_mongo():
            if self._client is None:
                self._client = MongoClient(self.uri, serverSelectionTimeoutMS=5000)
                self._db = self._client[self.db_name]
        else:
            _warn_fallback_once(self._sqlite_path)
            self._sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            if self._client is None:
                self._client = FallbackMongoClient(str(self._sqlite_path))
                self._db = self._client[self.db_name]

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
            self._db = None

    @property
    def client(self) -> Any:
        if self._client is None:
            self.connect()
        assert self._client is not None
        return self._client

    @property
    def db(self) -> Any:
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

    def save_tick(self, tick_doc: dict[str, Any]) -> None:
        """Save a single tick document (e.g. from live poll)."""
        self.db.price_history.insert_one(tick_doc)

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
