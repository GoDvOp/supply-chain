from copy import deepcopy
from datetime import date
from importlib.resources import files
import json

from jsonschema import Draft202012Validator

from .errors import DataError
from .queue import NUMERIC_PARAMETERS, nonnegative, validate_threshold


def effective(dataset, mode, config):
    if mode not in {"evidence", "experiment"}:
        raise DataError("mode должен быть evidence или experiment")
    schema = json.loads(files("supply_risk").joinpath("schemas/experiment.json").read_text())
    errors = list(Draft202012Validator(schema).iter_errors(config))
    if errors:
        raise DataError(f"Конфигурация эксперимента: {errors[0].message}")
    if mode == "evidence" and config:
        raise DataError("Экспериментальные параметры нельзя применять в evidence")
    conditions = {}
    for c in dataset["entities"]:
        if c["label"] != "Condition":
            continue
        conditions[c["id"]] = deepcopy(c)
        if mode == "evidence" and c["origin"] != "source_supported":
            conditions[c["id"]]["value"] = "unknown"
    parameters = {k: deepcopy(p["value"]) if mode == "experiment" or p["origin"] == "source_supported" else None
                  for k, p in dataset["parameters"].items()}
    overrides = []
    for kind, target in (("conditions", conditions), ("parameters", parameters)):
        for key, change in config.get(kind, {}).items():
            if key not in target:
                raise DataError(f"Неизвестный {kind}: {key}")
            old = target[key]["value"] if kind == "conditions" else target[key]
            overrides.append({"condition_id" if kind == "conditions" else "parameter_id": key,
                              "old_value": old, "new_value": deepcopy(change["value"]),
                              "origin": "эксперимент", "explanation": change["explanation"]})
            if kind == "conditions":
                target[key]["base_evidence_ids"] = target[key]["evidence_ids"]
                target[key]["evidence_ids"] = []
                target[key]["value"] = change["value"]
                target[key]["origin"] = "experiment"
            else:
                target[key] = deepcopy(change["value"])
    for key in NUMERIC_PARAMETERS:
        if parameters.get(key) is not None:
            nonnegative(parameters[key], key)
    validate_threshold(parameters.get("threshold"))
    return conditions, parameters, overrides


def state(values):
    if any(v is False for v in values):
        return "blocked"
    if any(v == "unknown" for v in values):
        return "conditional"
    return "applicable_in_scenario"


def assertion_conditions(assertion, mode, as_of):
    conditions, limitations = [], []
    if assertion["status"] == "assumption" and mode == "evidence":
        conditions.append({"id": f"assertion:{assertion['id']}", "value": "unknown",
                           "origin": "assumption", "evidence_ids": [],
                           "description": "Связь является допущением"})
    if assertion["status"] == "synthetic" and mode == "evidence":
        conditions.append({"id": f"synthetic:{assertion['id']}", "value": "unknown",
                           "origin": "synthetic", "evidence_ids": [],
                           "description": "Синтетический пример доступен как эксперимент"})
    if assertion.get("conflicts_with"):
        conditions.append({"id": f"conflict:{assertion['id']}", "value": "unknown",
                           "origin": "conflict", "evidence_ids": assertion["evidence_ids"],
                           "description": "Несовместимые утверждения требуют проверки"})
        limitations.append(f"Конфликт {assertion['id']}: {', '.join(assertion['conflicts_with'])}")
    if assertion.get("as_of_date") and assertion.get("review_after_days"):
        age = (date.fromisoformat(as_of) - date.fromisoformat(assertion["as_of_date"])).days
        if age > assertion["review_after_days"]:
            limitations.append(f"{assertion['id']}: требуется проверка актуальности; исторический факт сохранён")
    return conditions, limitations


def evaluate_paths(dataset, scenario, candidates, conditions, mode, as_of):
    index = {e["id"]: e for e in dataset["entities"]}
    rules = [r for r in dataset["rules"] if r["id"] in scenario["rule_ids"]]
    paths = []
    for candidate in candidates["paths"]:
        transitions, combined, evidence, limitations = [], {}, set(), []
        for i, edge in enumerate(candidate["edges"]):
            forward = edge["subject"] == candidate["nodes"][i]
            matching = [r for r in rules if edge["assertion_id"] in r["assertion_ids"] and
                        r["direction"] == ("forward" if forward else "reverse")]
            if not matching:
                break
            assertion = index[edge["assertion_id"]]
            extras, notes = assertion_conditions(assertion, mode, as_of)
            cs = [deepcopy(conditions[c]) for r in matching for c in r["condition_ids"]] + extras
            for c in cs:
                combined[c["id"]] = c
                evidence.update(c["evidence_ids"])
            evidence.update(assertion["evidence_ids"])
            limitations += notes
            transitions.append({**edge, "direction": "forward" if forward else "reverse",
                                "rule_ids": [r["id"] for r in matching], "conditions": cs,
                                "evidence_ids": assertion["evidence_ids"], "scope_note": assertion["scope_note"],
                                "assertion_status": assertion["status"], "limitations": notes})
        else:
            cs = list(combined.values())
            status = state([c["value"] for c in cs])
            paths.append({"nodes": [{"id": n, "name": index[n]["name"], "label": index[n]["label"]}
                                    for n in candidate["nodes"]],
                          "process_id": candidate["nodes"][-1], "transitions": transitions,
                          "status": status, "conditions": cs,
                          "blocking_conditions": [c["id"] for c in cs if c["value"] is False],
                          "evidence_ids": sorted(evidence), "limitations": sorted(set(limitations)),
                          "depends_on_assumptions": any(c["origin"] != "source_supported" for c in cs) or
                                                    any(t["assertion_status"] != "source_supported" for t in transitions)})
    return paths


def dependency_effects(dataset, scenario, conditions, mode, as_of):
    """Three-valued failure propagation for explicitly modelled service dependency groups."""
    index = {e["id"]: e for e in dataset["entities"]}
    rules = [r for r in dataset["rules"] if r["id"] in scenario["rule_ids"]]
    groups = {}
    for group in dataset["dependency_groups"]:
        groups.setdefault(group["consumer_id"], []).append(group)
    def all3(values):
        return False if False in values else "unknown" if "unknown" in values else True
    def any3(values):
        return True if True in values else "unknown" if "unknown" in values else False
    def failure(node_id, visited):
        if node_id == scenario["affected_service_id"]:
            return True
        if node_id in visited:
            return "unknown"
        gs = groups.get(node_id, [])
        if not gs:
            return False
        group_failures = []
        for group in gs:
            failures = []
            for service_id in group["service_ids"]:
                assertions = [e for e in index.values() if e["label"] == "Assertion" and
                              e["subject_id"] == node_id and e["object_id"] == service_id and
                              e["predicate"] in {"USES", "DEPENDS_ON"}]
                alternatives = []
                for a in assertions:
                    rr = [r for r in rules if a["id"] in r["assertion_ids"] and r["direction"] == "reverse"]
                    if not rr:
                        alternatives.append("unknown")
                        continue
                    extras, _ = assertion_conditions(a, mode, as_of)
                    vals = [conditions[c]["value"] for r in rr for c in r["condition_ids"]] + [e["value"] for e in extras]
                    alternatives.append(all3([failure(service_id, visited | {node_id}), all3(vals)]))
                failures.append(any3(alternatives) if alternatives else "unknown")
            group_failures.append(all3(failures) if group["operator"] == "OR" else any3(failures))
        return any3(group_failures)
    result = {}
    for consumer in sorted(groups):
        failed = failure(consumer, set())
        member_states = {s: failure(s, {consumer}) for g in groups[consumer] for s in g["service_ids"]}
        result[consumer] = {"fully_unavailable": failed,
                            "state": "unavailable" if failed is True else "conditional" if failed == "unknown" else
                                     "degraded" if any(v is True for v in member_states.values()) else "available",
                            "member_failures": member_states, "groups": groups[consumer]}
    return result
