from unittest.mock import MagicMock
from glassledger.diagnostics.differ import Differ


def build_mock_mongo_for_runs(run_1_docs: list[dict], run_2_docs: list[dict]):
    mongo = MagicMock()
    
    def mock_find(filter_query, projection=None):
        regex = filter_query.get("decision_id", {}).get("$regex", "")
        cursor = MagicMock()
        if "run_001" in regex:
            cursor.__iter__.return_value = iter(run_1_docs)
        elif "run_002" in regex:
            cursor.__iter__.return_value = iter(run_2_docs)
        else:
            cursor.__iter__.return_value = iter([])
        return cursor

    mongo.db.strategy_execution_log.find.side_effect = mock_find
    return mongo


def test_diff_engine_identical_runs():
    """Proves diff engine flags identical runs with 100% parity."""
    docs = [
        {
            "decision_id": f"dec_run_001_{i:04d}",
            "evaluation_seq": i,
            "trigger_tick": {"event_id": f"evt_{i}", "price": "150.0000", "volume": 100},
            "indicator_state": {"rsi": "45.0000"},
            "signal": "HOLD",
            "order_id": None,
        }
        for i in range(1, 10)
    ]
    # Clone for run 2
    docs2 = [
        {**d, "decision_id": d["decision_id"].replace("run_001", "run_002")}
        for d in docs
    ]

    mongo = build_mock_mongo_for_runs(docs, docs2)
    differ = Differ(mongo=mongo)
    result = differ.compare_runs("run_001", "run_002")

    assert result["identical"] is True
    assert result["total_sequences_verified"] == 9


def test_diff_engine_detects_input_dropped_tick():
    """
    FR-9 Verification:
    Proves diff engine detects a dropped tick at sequence #5 and classifies it as INPUT.
    """
    docs1 = [
        {
            "decision_id": f"dec_run_001_{i:04d}",
            "evaluation_seq": i,
            "trigger_tick": {"event_id": f"evt_{i}", "price": "150.0000", "volume": 100},
            "indicator_state": {"rsi": "45.0000"},
            "signal": "HOLD",
            "order_id": None,
        }
        for i in range(1, 10)
    ]
    # Run 2 is missing sequence 5
    docs2 = [
        {**d, "decision_id": d["decision_id"].replace("run_001", "run_002")}
        for d in docs1 if d["evaluation_seq"] != 5
    ]

    mongo = build_mock_mongo_for_runs(docs1, docs2)
    differ = Differ(mongo=mongo)
    result = differ.compare_runs("run_001", "run_002")

    assert result["identical"] is False
    assert result["first_divergence_seq"] == 5
    assert result["classification"] == "INPUT"
    assert "Dropped/Missing tick" in result["reason"]


def test_diff_engine_detects_observation_delta():
    """Proves diff engine detects indicator calculation delta as OBSERVATION."""
    docs1 = [
        {
            "decision_id": "dec_run_001_0001",
            "evaluation_seq": 1,
            "trigger_tick": {"event_id": "evt_1", "price": "150.0000", "volume": 100},
            "indicator_state": {"rsi": "28.5000"},
            "signal": "HOLD",
            "order_id": None,
        }
    ]
    docs2 = [
        {
            "decision_id": "dec_run_002_0001",
            "evaluation_seq": 1,
            "trigger_tick": {"event_id": "evt_1", "price": "150.0000", "volume": 100},
            "indicator_state": {"rsi": "29.1000"},  # Indicator delta
            "signal": "HOLD",
            "order_id": None,
        }
    ]

    mongo = build_mock_mongo_for_runs(docs1, docs2)
    differ = Differ(mongo=mongo)
    result = differ.compare_runs("run_001", "run_002")

    assert result["identical"] is False
    assert result["first_divergence_seq"] == 1
    assert result["classification"] == "OBSERVATION"


def test_diff_engine_detects_decision_flip():
    """Proves diff engine detects signal flip as DECISION."""
    docs1 = [
        {
            "decision_id": "dec_run_001_0001",
            "evaluation_seq": 1,
            "trigger_tick": {"event_id": "evt_1", "price": "150.0000", "volume": 100},
            "indicator_state": {"rsi": "28.5000"},
            "signal": "BUY",
            "order_id": "ord_1",
        }
    ]
    docs2 = [
        {
            "decision_id": "dec_run_002_0001",
            "evaluation_seq": 1,
            "trigger_tick": {"event_id": "evt_1", "price": "150.0000", "volume": 100},
            "indicator_state": {"rsi": "28.5000"},
            "signal": "HOLD",  # Signal flip
            "order_id": None,
        }
    ]

    mongo = build_mock_mongo_for_runs(docs1, docs2)
    differ = Differ(mongo=mongo)
    result = differ.compare_runs("run_001", "run_002")

    assert result["identical"] is False
    assert result["first_divergence_seq"] == 1
    assert result["classification"] == "DECISION"
