from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from . import MODEL_VERSION, RULES_VERSION
from .errors import DataError
from .loader import digest
from .queue import calculate
from .rules import dependency_effects, effective, evaluate_paths


def scenario_by_id(dataset, scenario_id):
    result = next((e for e in dataset["entities"] if e["id"] == scenario_id and e["label"] == "Scenario"), None)
    if result is None:
        raise DataError(f"Неизвестный сценарий: {scenario_id}")
    return result


def calculation_model(scenario):
    return scenario.get("calculation_model", "edi_queue" if scenario["incident_type"] == "availability" else "qualitative")


def qualitative(dataset, scenario, params, mode):
    required = ["volume"] if scenario["incident_type"] == "disclosure" else ["volume", "change_scale", "recoverable"]
    policy = dataset["severity_policy"]
    if (not scenario["data_category_ids"] or any(params.get(k) is None for k in required) or
        any(c not in policy["categories"] for c in scenario["data_category_ids"]) or
        (mode == "evidence" and policy["origin"] != "source_supported")):
        return {"severity": "unknown_severity", "missing": [k for k in required if params.get(k) is None],
                "data_category_ids": scenario["data_category_ids"], "policy_origin": policy["origin"]}
    volume = params["volume"]
    categories = [policy["categories"][c] for c in scenario["data_category_ids"]]
    severe = any(volume > c["volume_second"] or (scenario["incident_type"] != "disclosure" and
                 (params["recoverable"] is False or params["change_scale"] > c["change_scale_second"])) for c in categories)
    substantial = any(volume > c["volume_first"] for c in categories)
    return {"severity": "severe" if severe else "substantial" if substantial else "limited",
            "data_category_ids": scenario["data_category_ids"], "volume": volume,
            "change_scale": params.get("change_scale"), "recoverable": params.get("recoverable"),
            "scale_origin": policy["origin"], "policy": policy, "scope": "Только категории и контур указанного компонента"}


def core(dataset, scenario, candidates, mode, config, as_of, baseline_mode=None):
    conditions, parameters, overrides = effective(dataset, mode, config)
    baseline_mode = baseline_mode or mode
    paths = evaluate_paths(dataset, scenario, candidates, conditions, baseline_mode, as_of)
    groups = dependency_effects(dataset, scenario, conditions, baseline_mode, as_of) if scenario["incident_type"] == "availability" else {}
    index = {e["id"]: e for e in dataset["entities"]}
    processes, calculations = [], {}
    for pid in sorted({p["process_id"] for p in paths}):
        pp = [p for p in paths if p["process_id"] == pid]
        statuses = {p["status"] for p in pp}
        status = next(s for s in ("applicable_in_scenario", "conditional", "blocked") if s in statuses)
        process_threshold = index[pid]["threshold"]
        if baseline_mode == "evidence" and index[pid]["threshold_origin"] != "source_supported":
            process_threshold = None
        model = calculation_model(scenario)
        if model == "edi_queue":
            calc = calculate(parameters, parameters.get("threshold") or process_threshold)
        elif model == "qualitative":
            calc = qualitative(dataset, scenario, parameters, baseline_mode)
        else:
            calc = {"status": "not_modelled", "severity": "unknown_severity",
                    "message": "Количественная модель для этого сценария не задана; наблюдаемые последствия приведены отдельно."}
        calculations[pid] = calc
        protection = groups.get(pid)
        impacted = status == "applicable_in_scenario" and (not protection or protection["fully_unavailable"] is True)
        processes.append({"process_id": pid, "name": index[pid]["name"], "path_count": len(pp),
                          "status": status, "impact_confirmed_in_scenario": impacted,
                          "severity": calc["severity"] if impacted else "unknown_severity",
                          "potential_severity": calc["severity"], "dependency_evaluation": protection,
                          "calculation_scope": "Наблюдаемые последствия приведены отдельно; количественная модель не задана" if model == "none" else
                                               "Условный расчёт для затронутого потока; применимость указана отдельно",
                          "data_category_ids": scenario["data_category_ids"],
                          "depends_on_assumptions": any(p["depends_on_assumptions"] for p in pp) or bool(overrides)})
    used = {c["id"] for p in paths for c in p["conditions"]}
    return {"effective_parameters": parameters, "overrides": overrides, "paths": paths,
            "affected_processes": processes, "calculations": calculations, "dependency_evaluation": groups,
            "unknowns": [deepcopy(conditions[c]) for c in sorted(used & conditions.keys()) if conditions[c]["value"] == "unknown"]}


def signature(result):
    return {"paths": [{"nodes": [n["id"] for n in p["nodes"]],
                       "assertion_ids": [t["assertion_id"] for t in p["transitions"]],
                       "status": p["status"]} for p in result["paths"]],
            "processes": [{k: p[k] for k in ("process_id", "status", "severity", "potential_severity", "impact_confirmed_in_scenario")}
                          for p in result["affected_processes"]],
            "calculations": result["calculations"], "dependency_evaluation": result["dependency_evaluation"]}


def changed_paths(true_result, false_result):
    """Match routes by nodes AND assertions, including parallel graph edges."""
    def key(path):
        return (tuple(n["id"] for n in path["nodes"]),
                tuple(t["assertion_id"] for t in path["transitions"]))
    true_paths = {key(p): p for p in true_result["paths"]}
    false_paths = {key(p): p for p in false_result["paths"]}
    changes = []
    for identity in sorted(true_paths.keys() | false_paths.keys()):
        a, b = true_paths.get(identity), false_paths.get(identity)
        true_status = a["status"] if a else None
        false_status = b["status"] if b else None
        if true_status != false_status:
            changes.append({"nodes": list(identity[0]), "assertion_ids": list(identity[1]),
                            "process_id": (a or b)["process_id"],
                            "true_status": true_status, "false_status": false_status})
    return changes


def priorities(dataset, scenario, candidates, mode, config, as_of, result):
    output = []
    for condition in result["unknowns"]:
        outcomes = []
        for value in (True, False):
            trial = deepcopy(config)
            trial.setdefault("conditions", {})[condition["id"]] = {
                "value": value, "explanation": "Однофакторная проверка значимости неизвестного"}
            # Prevent unconfirmed baseline values leaking into an evidence counterfactual.
            if mode == "evidence":
                for c in dataset["entities"]:
                    if c["label"] == "Condition" and c["origin"] != "source_supported" and c["id"] != condition["id"]:
                        trial["conditions"][c["id"]] = {"value": "unknown", "explanation": "Сохраняется evidence baseline"}
                for key, p in dataset["parameters"].items():
                    if p["origin"] != "source_supported":
                        trial.setdefault("parameters", {})[key] = {"value": None, "explanation": "Сохраняется evidence baseline"}
            outcomes.append(core(dataset, scenario, candidates, "experiment", trial, as_of, baseline_mode=mode))
        a, b = (signature(v) for v in outcomes)
        changed = a != b
        route_changes = changed_paths(*outcomes)
        severe_path = False
        for outcome, status_key in zip(outcomes, ("true_status", "false_status")):
            processes = {p["process_id"]: p for p in outcome["affected_processes"]}
            for change in route_changes:
                process = processes.get(change["process_id"])
                if (change[status_key] == "applicable_in_scenario" and process is not None and
                    process["potential_severity"] == "severe" and
                    (not process["dependency_evaluation"] or
                     process["dependency_evaluation"]["fully_unavailable"] is True)):
                    severe_path = True
        threshold_changed = ({pid: c.get("threshold_exceeded") for pid, c in a["calculations"].items()} !=
                             {pid: c.get("threshold_exceeded") for pid, c in b["calculations"].items()})
        relevant_paths = [p for p in outcomes[0]["paths"] if
                          any(c["id"] == condition["id"] for c in p["conditions"])]
        unknown_obstruction = any(p["status"] == "conditional" for p in relevant_paths)
        priority = "P1" if severe_path else "P2" if route_changes or a["processes"] != b["processes"] or threshold_changed else "P3" if changed else None
        reason = ("Изменяется путь к тяжёлым последствиям" if priority == "P1" else
                  "Изменяется применимость пути или категория последствий" if priority == "P2" else
                  "Изменяются показатели" if priority == "P3" else
                  "Не удалось определить" if unknown_obstruction else
                  "Влияние не выявлено при остальных текущих условиях")
        output.append({"condition_id": condition["id"], "priority": priority, "reason": reason,
                       "changed_paths": route_changes,
                       "mode": "experiment", "true_result": a, "false_result": b,
                       "public_source_hint": condition["public_source_hint"],
                       "limitations": ["Другие unknown могут мешать оценке последствий"] if unknown_obstruction else []})
    return sorted(output, key=lambda x: (x["priority"] or "Z", x["condition_id"]))


def assess(dataset, scenario_id, candidates, mode, config=None, as_of=None, include_priorities=True):
    config = {} if config is None else config
    scenario = scenario_by_id(dataset, scenario_id)
    now = datetime.now(timezone.utc)
    as_of = as_of or now.date().isoformat()
    try:
        from datetime import date
        date.fromisoformat(as_of)
    except (ValueError, TypeError) as exc:
        raise DataError("as_of должен иметь формат YYYY-MM-DD") from exc
    result = core(dataset, scenario, candidates, mode, config, as_of)
    observations = [deepcopy(e) for e in dataset["entities"] if
                    e["label"] == "Observation" and e["scenario_id"] == scenario_id]
    overridden = {o["parameter_id"] for o in result["overrides"] if "parameter_id" in o}
    parameter_provenance = {key: {"value": value, "origin": "experiment" if key in overridden else dataset["parameters"][key]["origin"],
                                 "evidence_ids": [] if key in overridden else dataset["parameters"][key]["evidence_ids"],
                                 "base_evidence_ids": dataset["parameters"][key]["evidence_ids"]}
                            for key, value in result["effective_parameters"].items()}
    evidence_ids = sorted({e for p in result["paths"] for e in p["evidence_ids"]} |
                          {e for p in parameter_provenance.values() if p["value"] is not None for e in p["evidence_ids"]} |
                          {e for o in observations for e in o["evidence_ids"]})
    limitations = ["Оценка условна относительно исходного инцидента; вероятность атаки не вычисляется.",
                   "Меняется одно unknown за раз; взаимодействие неизвестных может скрыть эффект."]
    if calculation_model(scenario) == "edi_queue":
        limitations.append("Очередь: постоянный поток, нулевой начальный остаток, нет потерь и повторов; обход действует с начала сбоя.")
    if scenario.get("analysis_context") == "historical_reconstruction":
        limitations.append("Историческая реконструкция ограничена датой и объёмом раскрытых сведений. Наблюдения отражают заявления источников, а не независимую экспертизу. Эксперименты не изменяют прошлые последствия и не подтверждают текущий доступ или уязвимость.")
    if dataset["synthetic"]:
        limitations.append("Синтетический пример; выводы не переносятся на Ленту.")
    if calculation_model(scenario) == "qualitative":
        limitations.append("Шкала конфиденциальности/целостности задана в severity_policy набора по категориям. Демонстрационная шкала — проектная гипотеза; требуются все входы.")
    if candidates["incomplete"]:
        limitations.append("Поиск неполон: достигнута граница max_depth или max_paths.")
    if not result["paths"]:
        limitations.append("Путь не найден в данном наборе")
    limitations += sorted({x for p in result["paths"] for x in p["limitations"]})
    passport = {"run_id": str(uuid4()), "created_at": now.isoformat(), "as_of": as_of,
                "dataset_id": dataset["dataset_id"], "dataset_version": dataset["version"],
                "dataset_hash": dataset["sha256"], "model_version": MODEL_VERSION, "rules_version": RULES_VERSION,
                "mode": mode, "scenario_id": scenario_id, "incident": scenario,
                "analysis_context": scenario.get("analysis_context", "hypothetical"),
                "calculation_model": calculation_model(scenario), "observations": observations,
                "parameter_provenance": parameter_provenance,
                "search": {k: v for k, v in candidates.items() if k != "paths"},
                "evidence_ids": evidence_ids,
                "sources": [e for e in dataset["entities"] if e["id"] in evidence_ids],
                "facts": [e for e in dataset["entities"] if e["label"] == "Assertion" and
                          e["id"] in {t["assertion_id"] for p in result["paths"] for t in p["transitions"]}],
                "limitations": limitations}
    passport["input_hash"] = digest({"dataset_hash": dataset["sha256"], "mode": mode,
                                     "scenario": scenario_id, "config": config, "rules": RULES_VERSION,
                                     "model": MODEL_VERSION, "as_of": as_of, "search": passport["search"]})
    result["clarification_priorities"] = priorities(dataset, scenario, candidates, mode, config, as_of, result) if include_priorities else []
    return {**passport, **result}
