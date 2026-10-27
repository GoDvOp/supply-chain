import math

from .errors import DataError

NUMERIC_PARAMETERS = {"lambda", "T", "mu_b", "mu_r", "volume", "change_scale"}


def nonnegative(value, name):
    try:
        valid = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise DataError(f"{name}: требуется конечное неотрицательное число")
    return value


def validate_threshold(threshold):
    if threshold is None:
        return
    if not isinstance(threshold, dict) or set(threshold) != {"metric", "first", "second", "origin"}:
        raise DataError("threshold: нужны metric, first, second, origin")
    if threshold["metric"] not in {"T", "T_clear", "total_time"} or not threshold["origin"]:
        raise DataError("Некорректная метрика или происхождение порога")
    first = nonnegative(threshold["first"], "threshold.first")
    second = nonnegative(threshold["second"], "threshold.second")
    if first >= second:
        raise DataError("Пороги должны строго возрастать")


def calculate(parameters, threshold=None):
    validate_threshold(threshold)
    for k in ("lambda", "T", "mu_b", "mu_r"):
        if parameters.get(k) is not None:
            nonnegative(parameters[k], k)
    missing = [k for k in ("lambda", "T", "mu_b", "mu_r") if parameters.get(k) is None]
    if missing:
        return {"status": "insufficient_data", "message": "Недостаточно данных", "missing": missing,
                "severity": "unknown_severity", "threshold_exceeded": None}
    arrival, duration, bypass, recovery = (parameters[k] for k in ("lambda", "T", "mu_b", "mu_r"))
    q = max(0, arrival - bypass) * duration
    clear = 0 if q == 0 else q / (recovery - arrival) if recovery > arrival else None
    total = None if clear is None else duration + clear
    nonnegative(q, "Q (возможное переполнение)")
    if clear is not None:
        nonnegative(clear, "T_clear (возможное переполнение)")
        nonnegative(total, "total_time (возможное переполнение)")
    result = {"status": "non_draining" if clear is None else "draining", "Q": q, "T": duration,
              "T_clear": clear, "total_time": total,
              "queue_behavior": "growing" if clear is None and recovery < arrival else
                                "constant" if clear is None else "clears",
              "severity": "unknown_severity", "threshold_exceeded": None, "threshold": threshold}
    if threshold is not None:
        value = result[threshold["metric"]]
        result["threshold_exceeded"] = value is None or value > threshold["first"]
        result["severity"] = "severe" if clear is None or value > threshold["second"] else (
            "substantial" if value > threshold["first"] else "limited")
    return result
