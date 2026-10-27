import hashlib
import json
from importlib.resources import files

from jsonschema import Draft202012Validator, FormatChecker

from .errors import DataError
from .queue import NUMERIC_PARAMETERS, nonnegative, validate_threshold

RELATIONS = {"DEVELOPS", "PROVIDES", "USES", "DEPENDS_ON", "OWNS_PROCESS", "IMPLEMENTED"}
LABELS = {"Organization", "Service", "Process", "DataCategory", "Assertion", "Source", "Scenario", "Condition", "Observation"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def read_json(path):
    def reject_duplicate(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise DataError(f"Повторяющийся ключ JSON: {key}")
            obj[key] = value
        return obj
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle, object_pairs_hook=reject_duplicate,
                             parse_constant=lambda v: (_ for _ in ()).throw(DataError(f"Недопустимо: {v}")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DataError(f"Не удалось прочитать JSON {path}: {exc}") from exc


def validate(dataset):
    schema = json.loads(files("supply_risk").joinpath("schemas/dataset.json").read_text())
    errors = sorted(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(dataset),
                    key=lambda e: str(list(e.path)))
    if errors:
        e = errors[0]
        raise DataError(f"JSON Schema {'/'.join(map(str, e.path))}: {e.message}")
    entities = dataset["entities"]
    ids = [e["id"] for e in entities]
    if len(ids) != len(set(ids)):
        raise DataError("Идентификаторы объектов должны быть уникальны во всём наборе")
    index = {e["id"]: e for e in entities}
    def ref(value, labels=None):
        if value not in index or (labels and index[value]["label"] not in labels):
            raise DataError(f"Несуществующая ссылка или неверный тип: {value}")
    rules = {r["id"]: r for r in dataset["rules"]}
    if len(rules) != len(dataset["rules"]):
        raise DataError("Повторяющиеся правила")
    for e in entities:
        if e["dataset_id"] != dataset["dataset_id"]:
            raise DataError("Объект из другого dataset_id")
        label = e["label"]
        if dataset["synthetic"] is False and (e.get("synthetic") is True or e.get("status") == "synthetic"):
            raise DataError("Синтетические факты нельзя помещать в реальный набор")
        if label == "Assertion":
            ref(e["subject_id"], {"Organization", "Service", "Process", "DataCategory"})
            ref(e["object_id"], {"Organization", "Service", "Process", "DataCategory"})
            for source in e["evidence_ids"]:
                ref(source, {"Source"})
            if e["status"] == "source_supported" and not e["evidence_ids"]:
                raise DataError(f"Утверждение {e['id']} не имеет источника")
            for conflict in e.get("conflicts_with", []):
                ref(conflict, {"Assertion"})
        elif label == "Process":
            ref(e["owner_id"], {"Organization"})
            validate_threshold(e["threshold"])
            for source in e["threshold_evidence_ids"]:
                ref(source, {"Source"})
            if e["threshold"] is not None and e["threshold_origin"] == "source_supported" and not e["threshold_evidence_ids"]:
                raise DataError("Подтверждённый порог должен иметь источник")
        elif label == "Condition":
            for source in e["evidence_ids"]:
                ref(source, {"Source"})
            if e["origin"] == "source_supported" and not e["evidence_ids"]:
                raise DataError("Подтверждённое условие должно иметь источник")
        elif label == "Scenario":
            ref(e["origin_id"], {"Organization", "Service"})
            ref(e["affected_service_id"], {"Service"})
            for rule_id in e["rule_ids"]:
                if rule_id not in rules:
                    raise DataError(f"Неизвестное правило {rule_id}")
            for category in e["data_category_ids"]:
                ref(category, {"DataCategory"})
            if e.get("analysis_context") == "historical_reconstruction" and not e.get("incident_date"):
                raise DataError("Для исторического сценария нужна дата рассматриваемого инцидента")
            if e.get("calculation_model") == "edi_queue" and e["incident_type"] != "availability":
                raise DataError("Модель очереди сообщений допустима только для доступности")
        elif label == "Observation":
            ref(e["scenario_id"], {"Scenario"})
            ref(e["process_id"], {"Process"})
            scenario = index[e["scenario_id"]]
            if scenario.get("analysis_context") != "historical_reconstruction":
                raise DataError("Наблюдаемое последствие должно относиться к историческому сценарию")
            if not e["evidence_ids"]:
                raise DataError("Наблюдаемое последствие должно иметь источник")
            for source in e["evidence_ids"]:
                ref(source, {"Source"})
            if e["value"] is not None:
                nonnegative(e["value"], e["id"])
    for rule in rules.values():
        for a in rule["assertion_ids"]:
            ref(a, {"Assertion"})
        for c in rule["condition_ids"]:
            ref(c, {"Condition"})
    for scenario in (e for e in entities if e["label"] == "Scenario"):
        transitions = []
        for rid in scenario["rule_ids"]:
            transitions += [(aid, rules[rid]["direction"]) for aid in rules[rid]["assertion_ids"]]
        if len(transitions) != len(set(transitions)):
            raise DataError("Несколько правил для одного перехода: объедините обязательные условия в одно правило")
    for pid, p in dataset["parameters"].items():
        if pid in NUMERIC_PARAMETERS and p["value"] is not None:
            nonnegative(p["value"], pid)
        if p["origin"] == "source_supported" and not p["evidence_ids"]:
            raise DataError(f"Параметр {pid} не имеет источника")
        for source in p["evidence_ids"]:
            ref(source, {"Source"})
        if pid == "threshold":
            validate_threshold(p["value"])
    for group in dataset["dependency_groups"]:
        ref(group["consumer_id"], {"Process", "Service"})
        for service in group["service_ids"]:
            ref(service, {"Service"})
            if not any(e["label"] == "Assertion" and e["predicate"] in {"USES", "DEPENDS_ON"}
                       and e["subject_id"] == group["consumer_id"] and e["object_id"] == service for e in entities):
                raise DataError("Группа зависимости не подкреплена ребром")
    group_ids = [g["id"] for g in dataset["dependency_groups"]]
    if len(group_ids) != len(set(group_ids)):
        raise DataError("Идентификаторы групп зависимости должны быть уникальны")
    policy = dataset["severity_policy"]
    if policy["origin"] == "source_supported" and not policy["evidence_ids"]:
        raise DataError("Подтверждённая шкала должна иметь источник")
    for source in policy["evidence_ids"]:
        ref(source, {"Source"})
    for category, limits in policy["categories"].items():
        ref(category, {"DataCategory"})
        for key, value in limits.items():
            nonnegative(value, key)
        if limits["volume_first"] >= limits["volume_second"]:
            raise DataError("Пороги объёма должны строго возрастать")
    actual = digest({k: v for k, v in dataset.items() if k != "sha256"})
    if dataset["sha256"] != actual:
        raise DataError("SHA-256 набора не совпадает с каноническим JSON")
    return dataset


def load(path):
    return validate(read_json(path))


def seal(dataset):
    dataset["sha256"] = digest({k: v for k, v in dataset.items() if k != "sha256"})
    return dataset
