from copy import deepcopy
from uuid import uuid4

import pytest

from conftest import ROOT, direct_candidates
from supply_risk.compare import compare
from supply_risk.engine import assess, scenario_by_id, signature
from supply_risk.errors import DataError
from supply_risk.loader import canonical, load, read_json, seal, validate
from supply_risk.reports import markdown
from supply_risk.sensitivity import sensitivity_report


@pytest.mark.parametrize("name,scenario", [("rossiya", "R1"), ("pochta", "P1")])
def test_historical_reports_are_scoped_and_evidenced(name, scenario):
    data = load(ROOT / f"data/{name}.v1.json")
    report = assess(data, scenario, direct_candidates(data, scenario), "evidence", as_of="2026-10-26")
    assert report["analysis_context"] == "historical_reconstruction"
    assert report["paths"] and all(p["status"] == "applicable_in_scenario" for p in report["paths"])
    assert report["calculation_model"] == "none"
    assert all(c["status"] == "not_modelled" and "Q" not in c for c in report["calculations"].values())
    assert report["effective_parameters"]["T"] is None
    assert report["effective_parameters"]["volume"] is None
    assert report["observations"]
    source_ids = {s["id"] for s in report["sources"]}
    assert all(set(o["evidence_ids"]) <= source_ids for o in report["observations"])
    text = markdown(report)
    assert "Историческая реконструкция" in text
    assert "Наблюдаемые последствия и ответные действия" in text
    assert "денежных потерь" in text
    assert "Q = max" not in text
    import json
    assert markdown(json.loads(canonical(report))) == text


def test_rossiya_delays_are_snapshot_observations_not_outage_parameters():
    data = load(ROOT / "data/rossiya.v1.json")
    rows = [e for e in data["entities"] if e["label"] == "Observation" and e["metric"] == "flight_delay_minutes"]
    assert len(rows) == len({r["subject_ref"] for r in rows}) == 21
    assert max(r["value"] for r in rows) == 135
    assert next(r["value"] for r in rows if r["subject_ref"] == "FV6570") == 135
    assert all("11:55" in r["limitations"] and r["unit"] == "минут" for r in rows)
    assert all(p["value"] is None for p in data["parameters"].values())
    with pytest.raises(DataError, match="модель очереди"):
        sensitivity_report(data, "R1", direct_candidates(data, "R1"), read_json(ROOT / "experiments/grid.json"))


def test_pochta_does_not_invent_contractor_or_loss_and_preserves_history():
    data = load(ROOT / "data/pochta.v1.json")
    contractor = next(e for e in data["entities"] if e["id"] == "undisclosed_contractor")
    assert contractor["role"] == "unidentified_contractor"
    assert "страна" in contractor["scope_note"]
    candidates = direct_candidates(data, "P1")
    result = compare(data, "P1", candidates, {}, read_json(ROOT / "experiments/pochta_access_revoked.json"))
    assert result["before"]["paths"][0]["status"] == "applicable_in_scenario"
    assert result["after"]["paths"][0]["status"] == "blocked"
    assert result["before"]["observations"] == result["after"]["observations"]
    assert len(result["after"]["observations"]) == 4
    assert all(o["value"] is None for o in result["after"]["observations"])
    assert "Эксперименты не изменяют прошлые последствия" in markdown(result)


def test_observation_survives_missing_candidate_path():
    data = load(ROOT / "data/rossiya.v1.json")
    candidates = direct_candidates(data, "R1")
    candidates.update(paths=[], incomplete=True, depth_limit_reached=True)
    result = assess(data, "R1", candidates, "evidence")
    assert len(result["observations"]) == 23
    assert "src_rossiya_delays" in result["evidence_ids"]
    text = markdown(result)
    assert "Путь не найден в данном наборе" in text
    assert "Поиск неполон" in text
    assert "FV6570" in text and "135" in text


@pytest.mark.parametrize("mutation", ["reference", "evidence", "context", "date", "negative"])
def test_invalid_observation_is_rejected(mutation):
    data = load(ROOT / "data/pochta.v1.json")
    row = next(e for e in data["entities"] if e["label"] == "Observation")
    scenario = scenario_by_id(data, "P1")
    if mutation == "reference":
        row["process_id"] = "missing"
    elif mutation == "evidence":
        row["evidence_ids"] = []
    elif mutation == "context":
        scenario["analysis_context"] = "hypothetical"
    elif mutation == "date":
        scenario["incident_date"] = None
    else:
        row["value"] = -1
    with pytest.raises(DataError):
        validate(seal(data))


@pytest.fixture(params=[("rossiya", "R1"), ("pochta", "P1")])
def historical_database(request, db):
    name, scenario = request.param
    data = load(ROOT / f"data/{name}.v1.json")
    data["dataset_id"] = "test_history_" + uuid4().hex
    for e in data["entities"]:
        e["dataset_id"] = data["dataset_id"]
    seal(data)
    db.import_dataset(data)
    try:
        yield db, data, scenario
    finally:
        with db.driver.session() as session:
            session.run("MATCH (n {dataset_id:$ds}) DETACH DELETE n", ds=data["dataset_id"]).consume()


@pytest.mark.integration
def test_history_import_links_idempotency_and_restore(historical_database, dataset):
    db, data, scenario = historical_database
    candidates = db.candidates(data, scenario_by_id(data, scenario))
    before = assess(data, scenario, candidates, "evidence", as_of="2026-10-26")
    repeated = db.import_dataset(data)
    assert repeated["created_nodes"] == repeated["created_relationships"] == 0
    observations = [e for e in data["entities"] if e["label"] == "Observation"]
    with db.driver.session() as session:
        row = session.run(
            "MATCH (o:Observation {dataset_id:$ds})-[r:ABOUT_SCENARIO|OBSERVED_IN|SUPPORTED_BY]->(n) "
            "WHERE r.dataset_id=$ds AND r.version=$version AND n.dataset_id=$ds "
            "RETURN count(r) AS count", ds=data["dataset_id"], version=data["version"]).single()
        assert row["count"] == sum(2 + len(o["evidence_ids"]) for o in observations)
    exported = db.load(data["dataset_id"], data["version"])
    with db.driver.session() as session:
        session.run("MATCH (n {dataset_id:$ds}) DETACH DELETE n", ds=data["dataset_id"]).consume()
    db.import_dataset(exported)
    restored = db.load(data["dataset_id"], data["version"])
    after = assess(restored, scenario, db.candidates(restored, scenario_by_id(restored, scenario)), "evidence", as_of="2026-10-26")
    assert before["observations"] == after["observations"]
    assert signature(before) == signature(after)
    assert before["input_hash"] == after["input_hash"]
    assert not any(e["label"] == "Observation" for e in dataset["entities"])
