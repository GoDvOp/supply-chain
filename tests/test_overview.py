from copy import deepcopy
import json
from uuid import uuid4

import pytest

from conftest import ROOT
from supply_risk.engine import assess, scenario_by_id, signature
from supply_risk.loader import load, seal
from supply_risk.overview import COMPACT_QUERY, DETAIL_QUERY, build_overview, write_overview


def test_russia_summary_preserves_snapshot_meaning():
    data = load(ROOT / "data/rossiya.v1.json")
    original = deepcopy(data)
    summary, = build_overview(data)
    assert summary["observation_count"] == 23
    assert summary["known_flight_count"] == 21
    assert (summary["min_flight_delay_minutes"], summary["max_flight_delay_minutes"]) == (3, 135)
    assert summary["response_observation_count"] == 1
    assert "21" in summary["name"] and "135" in summary["name"]
    assert "Не итог суток" in summary["description"] and "11:55" in summary["description"]
    assert summary["derived"] and summary["dataset_hash"] == data["sha256"]
    assert data == original


def test_lenta_has_no_invented_historical_impacts(dataset):
    summaries = {s["process_id"]: s for s in build_overview(dataset)}
    assert set(summaries) == {"hr", "orders"}
    assert summaries["hr"]["scenario_ids"] == ["S1", "S2", "S4"]
    assert summaries["orders"]["scenario_ids"] == ["S3"]
    for summary in summaries.values():
        assert summary["context"] == "no_observations"
        assert summary["observation_count"] == 0
        assert summary["max_flight_delay_minutes"] is None
        assert "не означает отсутствие риска" in summary["description"]


def test_post_summary_does_not_turn_unknown_volume_into_zero():
    summary, = build_overview(load(ROOT / "data/pochta.v1.json"))
    assert summary["observation_count"] == 4
    assert summary["response_observation_count"] == 3
    assert "объём не установлен" in summary["name"]
    disclosure, = [m for m in summary["metrics"] if m["metric"] == "data_fragment_disclosed"]
    assert disclosure["numeric_count"] == 0 and disclosure["unknown_value_count"] == 1
    assert disclosure["minimum"] is disclosure["maximum"] is None


def test_incomplete_delay_values_do_not_claim_full_maximum():
    data = load(ROOT / "data/rossiya.v1.json")
    next(o for o in data["entities"] if o["id"] == "delay_FV6311")["value"] = None
    seal(data)
    summary, = build_overview(data)
    assert summary["known_flight_count"] == 21
    assert summary["max_flight_delay_minutes"] is None
    assert "неполны" in summary["name"]


def test_synthetic_overview_is_explicit(synthetic):
    summary, = build_overview(synthetic)
    assert summary["context"] == "synthetic"
    assert "Синтетический" in summary["name"]


@pytest.mark.parametrize("different", ["unit", "snapshot"])
def test_summary_does_not_combine_different_units_or_snapshots(different):
    data = load(ROOT / "data/rossiya.v1.json")
    row = next(e for e in data["entities"] if e["label"] == "Observation" and e["metric"] == "flight_delay_minutes")
    if different == "unit":
        row["unit"], row["value"] = "часов", 1
    else:
        source = deepcopy(next(e for e in data["entities"] if e["id"] == "src_rossiya_delays"))
        source["id"] = "another_snapshot"
        data["entities"].append(source)
        repeated = deepcopy(row)
        repeated.update(id="another_observation", value=500, evidence_ids=[source["id"]])
        data["entities"].append(repeated)
    seal(data)
    summary, = build_overview(data)
    assert summary["max_flight_delay_minutes"] is None
    assert summary["known_flight_count"] is None
    assert len([m for m in summary["metrics"] if m["metric"] == "flight_delay_minutes"]) == 2
    assert "Разные снимки не объединяются" in summary["description"]


def test_overview_files_keep_evidence_and_limits(tmp_path):
    data = load(ROOT / "data/rossiya.v1.json")
    prefix = tmp_path / "summary"
    write_overview(data, build_overview(data), prefix)
    text = prefix.with_suffix(".md").read_text()
    assert "21" in text and "135" in text and "11:55" in text
    assert "https://t.me/rossiya_airlines/981" in text
    saved = json.loads(prefix.with_suffix(".json").read_text())
    assert saved["dataset_hash"] == data["sha256"]
    assert "HAS_IMPACT_SUMMARY" in prefix.with_suffix(".cypher").read_text()


@pytest.fixture(params=["lenta", "rossiya", "pochta"])
def overview_db(request, db):
    data = load(ROOT / f"data/{request.param}.v1.json")
    data["dataset_id"] = "test_overview_" + uuid4().hex
    for entity in data["entities"]:
        entity["dataset_id"] = data["dataset_id"]
    seal(data)
    db.import_dataset(data)
    try:
        yield db, data
    finally:
        with db.driver.session() as session:
            session.run("MATCH (n {dataset_id:$ds}) DETACH DELETE n", ds=data["dataset_id"]).consume()


@pytest.mark.integration
def test_compact_view_and_refresh_are_isolated_and_idempotent(overview_db):
    db, data = overview_db
    first = db.refresh_overview(data["dataset_id"], "1")
    second = db.refresh_overview(data["dataset_id"], "1")
    assert first["summaries"] == second["summaries"] == build_overview(data)
    assert second["changes"]["created_nodes"] == second["changes"]["created_relationships"] == 0
    original_observations = [e for e in data["entities"] if e["label"] == "Observation"]
    with db.driver.session() as session:
        paths = [r["path"] for r in session.run(COMPACT_QUERY, datasets=[data["dataset_id"]], version="1")]
        nodes = {n.element_id: n for path in paths for n in path.nodes}
        assert sum("ImpactSummary" in n.labels for n in nodes.values()) == len(first["summaries"])
        assert not any("Observation" in n.labels for n in nodes.values())
        assert all(n["dataset_id"] == data["dataset_id"] for n in nodes.values())
        details = session.run(DETAIL_QUERY, datasets=[data["dataset_id"]], version="1").data()
        assert len(details) == len(original_observations)
        count = session.run("MATCH (o:Observation {dataset_id:$ds}) RETURN count(o) AS count",
                            ds=data["dataset_id"]).single()["count"]
        assert count == len(original_observations)
    assert db.load(data["dataset_id"], "1") == data


@pytest.mark.integration
def test_summary_versions_and_assessment_are_independent(overview_db):
    db, data = overview_db
    scenario = next(e for e in data["entities"] if e["label"] == "Scenario")
    before = assess(data, scenario["id"], db.candidates(data, scenario), "evidence", as_of="2026-10-26")
    original_summaries = db.refresh_overview(data["dataset_id"], "1")["summaries"]
    changed = deepcopy(data)
    changed["version"] = "2"
    next(e for e in changed["entities"] if e["label"] == "Process")["name"] += " v2"
    seal(changed)
    db.import_dataset(changed)
    second = db.refresh_overview(data["dataset_id"], "2")
    assert second["summaries"] == build_overview(changed)
    assert db.refresh_overview(data["dataset_id"], "1")["summaries"] == original_summaries
    after = assess(data, scenario["id"], db.candidates(data, scenario), "evidence", as_of="2026-10-26")
    assert signature(before) == signature(after)
    assert before["observations"] == after["observations"]
    assert before["input_hash"] == after["input_hash"]
