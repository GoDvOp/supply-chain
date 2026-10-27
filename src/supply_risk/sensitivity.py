from itertools import product
from math import prod
from copy import deepcopy

from .engine import assess, calculation_model, scenario_by_id
from .errors import DataError
from .loader import digest
from .queue import calculate, nonnegative, validate_threshold


def sensitivity(grid):
    allowed = {"lambda", "T", "mu_b", "mu_r", "threshold", "max_combinations"}
    if not isinstance(grid, dict) or set(grid) - allowed:
        raise DataError("Неизвестные поля сетки параметров")
    nonnegative(grid.get("lambda"), "lambda")
    validate_threshold(grid.get("threshold"))
    limit = grid.get("max_combinations", 1000)
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise DataError("max_combinations должен быть целым числом 1–1000")
    for key in ("T", "mu_b", "mu_r"):
        if not isinstance(grid.get(key), list) or not grid[key]:
            raise DataError(f"{key}: нужен непустой список чисел")
        for value in grid[key]:
            nonnegative(value, key)
    count = prod(len(grid[k]) for k in ("T", "mu_b", "mu_r"))
    if count > limit:
        raise DataError(f"Сетка содержит {count} комбинаций, лимит {limit}")
    rows = []
    for duration, bypass, recovery in product(grid["T"], grid["mu_b"], grid["mu_r"]):
        params = {"lambda": grid["lambda"], "T": duration, "mu_b": bypass, "mu_r": recovery}
        rows.append({"parameters": params, **calculate(params, grid.get("threshold"))})
    finite = [r["total_time"] for r in rows if r["status"] == "draining"]
    return {"mode": "experiment", "origin": "проектная гипотеза", "rows": rows,
            "combinations": count, "non_draining_count": sum(r["status"] == "non_draining" for r in rows),
            "finite_min": min(finite) if finite else None, "finite_max": max(finite) if finite else None,
            "finite_mean": sum(finite) / len(finite) if finite else None,
            "conclusion_changes_when": "Q>0 и mu_r<=lambda: non_draining; иначе время сравнивается только с порогом той же метрики.",
            "limitations": ["Экспериментальная сетка; не фактическая нагрузка Ленты."]}


def sensitivity_report(dataset, scenario_id, candidates, grid, as_of=None):
    if scenario_by_id(dataset, scenario_id)["incident_type"] != "availability":
        raise DataError("sensitivity применим к сценарию доступности")
    if calculation_model(scenario_by_id(dataset, scenario_id)) != "edi_queue":
        raise DataError("Для этого сценария модель очереди сообщений не применяется")
    results = sensitivity(grid)
    context = assess(dataset, scenario_id, candidates, "experiment", {}, as_of)
    for row in results["rows"]:
        values = {**row["parameters"], "threshold": grid.get("threshold")}
        row["overrides"] = [
            {"parameter_id": key, "old_value": deepcopy(context["effective_parameters"][key]),
             "new_value": deepcopy(value), "origin": "эксперимент",
             "explanation": "Параметр данной комбинации экспериментальной сетки."}
            for key, value in values.items()
        ]
    report = {**context, **results, "grid": deepcopy(grid),
              "grid_scope": "Контекст путей и приоритетов рассчитан на исходных значениях набора. "
                            "Числа каждой строки сетки — отдельный эксперимент для затронутого потока; "
                            "они не подтверждают применимость условного или заблокированного пути."}
    report["limitations"] = list(dict.fromkeys(context["limitations"] + results["limitations"]))
    report["input_hash"] = digest({"assessment_hash": context["input_hash"], "grid": grid})
    return report
