from .engine import assess, signature


def differences(before, after, prefix=""):
    if isinstance(before, dict) and isinstance(after, dict):
        rows = []
        for key in sorted(before.keys() | after.keys()):
            rows += differences(before.get(key), after.get(key), f"{prefix}.{key}" if prefix else key)
        return rows
    if before != after:
        return [{"field": prefix, "before": before, "after": after}]
    return []


def compare(dataset, scenario_id, candidates, before, after, as_of=None):
    a = assess(dataset, scenario_id, candidates, "experiment", before, as_of)
    b = assess(dataset, scenario_id, candidates, "experiment", after, a["as_of"])
    result = {k: a[k] for k in ("dataset_id", "dataset_version", "dataset_hash", "model_version", "rules_version", "scenario_id", "mode")}
    return {**result, "before": a, "after": b,
            "changes": differences({"conditions": before.get("conditions", {}), "parameters": before.get("parameters", {})},
                                   {"conditions": after.get("conditions", {}), "parameters": after.get("parameters", {})}),
            "outcome_changes": differences(signature(a), signature(b)),
            "limitations": ["Одинаковый снимок данных, правила, дата и параметры поиска; отличаются только перечисленные изменения."]}
