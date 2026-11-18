// GlassLedger MongoDB Collections & Schema Validation
// Run against database 'glassledger'

// 1. Time-series collection for ticks/bars
try {
  db.createCollection("price_history", {
    timeseries: {
      timeField: "ts",
      metaField: "symbol",
      granularity: "seconds"
    }
  });
} catch (e) {
  print("Collection price_history might already exist: " + e);
}
db.price_history.createIndex({ symbol: 1, ts: -1 });

// 2. Strategies Collection with JSON Schema Validation
try {
  db.createCollection("strategies", {
    validator: {
      $jsonSchema: {
        bsonType: "object",
        required: ["strategy_id", "investor_id", "strategy_type", "parameters", "version", "is_active"],
        properties: {
          strategy_id: { bsonType: "string" },
          investor_id: { bsonType: ["int", "long"] },
          strategy_type: { enum: ["MA_CROSSOVER", "RSI_THRESHOLD"] },
          version: { bsonType: "string" },
          is_active: { bsonType: "bool" },
          parameters: { bsonType: "object" }
        }
      }
    }
  });
} catch (e) {
  print("Collection strategies might already exist: " + e);
}
db.strategies.createIndex({ strategy_id: 1 }, { unique: true });
db.strategies.createIndex({ is_active: 1, strategy_type: 1 });

// 3. Strategy Execution Log (Immutable Decision Snapshots)
try {
  db.createCollection("strategy_execution_log", {
    validator: {
      $jsonSchema: {
        bsonType: "object",
        required: ["decision_id", "strategy_id", "evaluation_seq", "trigger_tick", "indicator_state", "signal"],
        properties: {
          decision_id: { bsonType: "string" },
          strategy_id: { bsonType: "string" },
          evaluation_seq: { bsonType: ["long", "int"] },
          trigger_tick: { bsonType: "object" },
          indicator_state: { bsonType: "object" },
          signal: { enum: ["BUY", "SELL", "HOLD"] },
          order_id: { bsonType: ["string", "null"] }
        }
      }
    }
  });
} catch (e) {
  print("Collection strategy_execution_log might already exist: " + e);
}
db.strategy_execution_log.createIndex({ decision_id: 1 }, { unique: true });
db.strategy_execution_log.createIndex({ strategy_id: 1, evaluation_seq: 1 }, { unique: true });
