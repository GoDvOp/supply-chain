"""Regressions for FR06 priorities and OUT02 sensitivity report context."""
from copy import deepcopy
import json
import os
import subprocess
import sys

import pytest

from conftest import ROOT, direct_candidates
from supply_risk.engine import assess
from supply_risk.loader import read_json, seal, validate
from supply_risk.reports import markdown


@pytest.mark.parametrize(
    "changed_severity,other_condition,expected",
    [("limited", None, "P2"), ("severe", None, "P1"),
     ("severe", "unknown", "P2"), ("severe", False, None)],
)
def test_priority_tracks_changed_path_not_unrelated_severe_process(
    dataset, changed_severity, other_condition, expected
):
    data = deepcopy(dataset)
    index = {e["id"]: e for e in data["entities"]}
    orders = index["orders"]
    orders["threshold"] = {
        "metric": "total_time", "first": 1, "second": 2, "origin": "проектная гипотеза"
    }
    orders["threshold_origin"] = "assumption"
    process = deepcopy(orders)
    process.update(id="changed_process", name="Процесс проверяемого пути")
    if changed_severity == "limited":
        process["threshold"].update(first=20, second=30)
    data["entities"].append(process)
    assertion = deepcopy(index["a_orders_uses"])
    assertion.update(id="changed_uses", subject_id=process["id"])
    data["entities"].append(assertion)
    for rule in data["rules"]:
        if rule["id"] == "r_edi_consumer":
            rule["condition_ids"] = []
    condition_ids = ["flow_affected"]
    if other_condition is not None:
        index["docs_present"]["value"] = other_condition
        condition_ids.append("docs_present")
    data["rules"].append({
        "id": "r_changed", "description": "Путь, зависящий от уточняемого условия",
        "assertion_ids": ["changed_uses"], "direction": "reverse", "condition_ids": condition_ids,
    })
    index["S3"]["rule_ids"].append("r_changed")
    validate(seal(data))
    config = read_json(ROOT / "experiments/edi_base.json")
    config["conditions"] = {}
    config["parameters"].pop("threshold")
    report = assess(data, "S3", direct_candidates(data, "S3"), "experiment", config, as_of="2026-10-26")
    priority = next(p for p in report["clarification_priorities"] if p["condition_id"] == "flow_affected")
    assert priority["priority"] == expected
    for side in ("true_result", "false_result"):
        unaffected = next(p for p in priority[side]["processes"] if p["process_id"] == "orders")
        assert unaffected["severity"] == "severe"
    if expected is not None:
        assert {p["process_id"] for p in priority["changed_paths"]} == {"changed_process"}
    else:
        assert priority["changed_paths"] == []
        assert priority["reason"] == "Влияние не выявлено при остальных текущих условиях"


def test_priority_distinguishes_parallel_assertions(dataset):
    data = deepcopy(dataset)
    index = {e["id"]: e for e in data["entities"]}
    assertion = deepcopy(index["a_orders_uses"])
    assertion["id"] = "parallel_uses"
    data["entities"].append(assertion)
    for rule in data["rules"]:
        if rule["id"] == "r_edi_consumer":
            rule["condition_ids"] = []
    data["rules"].append({
        "id": "r_parallel", "description": "Условный параллельный путь",
        "assertion_ids": ["parallel_uses"], "direction": "reverse",
        "condition_ids": ["flow_affected", "docs_present"],
    })
    index["S3"]["rule_ids"].append("r_parallel")
    validate(seal(data))
    config = read_json(ROOT / "experiments/edi_base.json")
    config["conditions"] = {}
    report = assess(data, "S3", direct_candidates(data, "S3"), "experiment", config, as_of="2026-10-26")
    priority = next(p for p in report["clarification_priorities"] if p["condition_id"] == "flow_affected")
    # A severe applicable route exists, but the changed parallel route remains conditional.
    assert priority["priority"] == "P2"
    assert len(priority["changed_paths"]) == 1
    assert priority["changed_paths"][0]["assertion_ids"][-1] == "parallel_uses"


def test_unrelated_unknown_does_not_obscure_known_blocker(dataset):
    data = deepcopy(dataset)
    index = {e["id"]: e for e in data["entities"]}
    index["docs_present"]["value"] = False
    assertion = deepcopy(index["a_orders_uses"])
    assertion["id"] = "blocked_parallel"
    data["entities"].append(assertion)
    for rule in data["rules"]:
        if rule["id"] == "r_edi_consumer":
            rule["condition_ids"] = ["active_access"]
    data["rules"].append({
        "id": "r_blocked_parallel", "description": "Заведомо заблокированный путь",
        "assertion_ids": ["blocked_parallel"], "direction": "reverse",
        "condition_ids": ["flow_affected", "docs_present"],
    })
    index["S3"]["rule_ids"].append("r_blocked_parallel")
    validate(seal(data))
    report = assess(data, "S3", direct_candidates(data, "S3"), "experiment")
    priority = next(p for p in report["clarification_priorities"] if p["condition_id"] == "flow_affected")
    assert priority["priority"] is None
    assert priority["reason"] == "Влияние не выявлено при остальных текущих условиях"
    assert priority["limitations"] == []


def test_sensitivity_keeps_context_sources_and_warnings(dataset):
    from supply_risk.sensitivity import sensitivity_report
    data = deepcopy(dataset)
    assertion = next(e for e in data["entities"] if e["id"] == "a_provides")
    assertion.update(as_of_date="2020-01-01", conflicts_with=["a_orders_uses"])
    seal(data)
    candidates = direct_candidates(data, "S3")
    candidates.update(incomplete=True, depth_limit_reached=True)
    grid = read_json(ROOT / "experiments/grid.json")
    original = deepcopy(grid)
    report = sensitivity_report(data, "S3", candidates, grid, as_of="2026-10-26")
    text = markdown(report)
    assert len(report["rows"]) == 27
    assert report["non_draining_count"] == 12
    assert text.count("| non_draining |") == 12
    for value in (
        report["incident"]["description"], report["input_hash"], report["dataset_hash"],
        "Факты и границы утверждений", "Допущения и переопределения", "Источники",
        "Неизвестные условия и приоритет уточнения", "Поиск неполон", "Конфликт",
        "актуальности", "Q = max(0, lambda", "flow_affected", "src_edi",
        "Превышен порог", report["conclusion_changes_when"],
    ):
        assert value in text
    assert any("Поиск неполон" in item for item in report["limitations"])
    assert any("Конфликт" in item for item in report["limitations"])
    assert any("актуальности" in item for item in report["limitations"])
    assert grid == original
    repeated = sensitivity_report(data, "S3", candidates, grid, as_of="2026-10-26")
    assert repeated["input_hash"] == report["input_hash"]
    assert repeated["rows"] == report["rows"]
    for row in report["rows"]:
        assert {o["parameter_id"] for o in row["overrides"]} == {"lambda", "T", "mu_b", "mu_r", "threshold"}
        assert all(o["origin"] == "эксперимент" for o in row["overrides"])


@pytest.mark.integration
def test_sensitivity_cli_retains_no_path_and_search_warning(imported, tmp_path):
    _, data = imported
    prefix = tmp_path / "sensitivity"
    result = subprocess.run([
        sys.executable, "-m", "supply_risk.cli", "sensitivity", "--dataset", data["dataset_id"],
        "--version", data["version"], "--scenario", "S3", "--max-depth", "1", "--grid",
        str(ROOT / "experiments/grid.json"), "--as-of", "2026-10-26", "--output", str(prefix),
    ], env=os.environ.copy(), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    report = json.loads(prefix.with_suffix(".json").read_text())
    text = prefix.with_suffix(".md").read_text()
    assert len(report["rows"]) == 27
    assert report["search"]["incomplete"]
    assert report["paths"] == []
    assert "Путь не найден в данном наборе" in text
    assert "Поиск неполон" in text
    assert "Риск отсутствует" not in text
    assert report["input_hash"] in text


@pytest.mark.parametrize("kind", ["assessment", "compare", "sensitivity"])
def test_markdown_is_stable_after_storage_json_roundtrip(dataset, kind):
    from supply_risk.compare import compare
    from supply_risk.loader import canonical
    from supply_risk.sensitivity import sensitivity_report
    candidates = direct_candidates(dataset, "S3")
    if kind == "sensitivity":
        report = sensitivity_report(dataset, "S3", candidates, read_json(ROOT / "experiments/grid.json"))
    elif kind == "compare":
        report = compare(dataset, "S3", candidates,
                         read_json(ROOT / "experiments/edi_base.json"),
                         read_json(ROOT / "experiments/edi_bypass.json"))
    else:
        report = assess(dataset, "S3", candidates, "evidence")
    restored = json.loads(canonical(report))
    assert markdown(restored) == markdown(report)
