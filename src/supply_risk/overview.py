"""Derived display summaries. These are not new facts or risk assessments."""
from collections import defaultdict
import json
from pathlib import Path
import re

from .errors import DataError
from .loader import validate

OVERVIEW_VERSION = "1.0.0"

OVERVIEW_STYLE = '''node.ImpactSummary {
  color: #4C78A8;
  size: 100px;
  caption: "{name}";
}
'''

COMPACT_QUERY = """MATCH path=(a:Entity)-[r:DEVELOPS|PROVIDES|USES|DEPENDS_ON|OWNS_PROCESS|IMPLEMENTED]->(b:Entity)
WHERE a.dataset_id IN $datasets AND b.dataset_id=a.dataset_id
  AND r.dataset_id=a.dataset_id AND r.version=$version
RETURN path
UNION ALL
MATCH path=(p:Process)-[r:HAS_IMPACT_SUMMARY]->(s:ImpactSummary)
WHERE p.dataset_id IN $datasets AND s.dataset_id=p.dataset_id
  AND r.dataset_id=p.dataset_id AND r.version=$version AND s.version=$version
RETURN path;"""

DETAIL_QUERY = """MATCH (s:ImpactSummary)-[r:SUMMARIZES]->(o:Observation)
WHERE s.dataset_id IN $datasets AND s.version=$version
  AND o.dataset_id=s.dataset_id AND r.dataset_id=s.dataset_id AND r.version=$version
RETURN s.dataset_id AS dataset, s.process_name AS process, o.subject_ref AS object,
       o.description AS observation, o.value AS value, o.unit AS unit,
       o.reported_on AS reported_on, o.limitations AS limitations
ORDER BY dataset, process, value DESC, object;"""


def build_overview(dataset):
    validate(dataset)
    entities = {e["id"]: e for e in dataset["entities"]}
    assertions = [e for e in entities.values() if e["label"] == "Assertion"]
    rules = {r["id"]: r for r in dataset["rules"]}
    output = []
    for process in sorted((e for e in entities.values() if e["label"] == "Process"), key=lambda p: p["id"]):
        observations = sorted((e for e in entities.values() if e["label"] == "Observation" and
                               e["process_id"] == process["id"]), key=lambda o: o["id"])
        scenario_ids = set()
        for scenario in (e for e in entities.values() if e["label"] == "Scenario"):
            aids = {a for rid in scenario["rule_ids"] for a in rules[rid]["assertion_ids"]}
            if any(a["id"] in aids and process["id"] in (a["subject_id"], a["object_id"]) for a in assertions):
                scenario_ids.add(scenario["id"])
        scenario_ids.update(o["scenario_id"] for o in observations)
        source_ids = {s for o in observations for s in o["evidence_ids"]}
        if not observations:
            source_ids.update(s for a in assertions if process["id"] in (a["subject_id"], a["object_id"])
                              for s in a["evidence_ids"])
        groups = defaultdict(list)
        for o in observations:
            # Different units, scenarios and source snapshots must stay distinct.
            groups[(o["scenario_id"], o["metric"], o["unit"], o["reported_on"], tuple(sorted(o["evidence_ids"])))].append(o)
        metrics = []
        for (scenario_id, metric, unit, reported_on, evidence), rows in sorted(groups.items()):
            values = [r["value"] for r in rows if r["value"] is not None]
            subjects = {r["subject_ref"] for r in rows if r["subject_ref"] is not None}
            metrics.append({"scenario_id": scenario_id, "metric": metric, "unit": unit,
                            "reported_on": reported_on, "evidence_ids": list(evidence),
                            "observation_count": len(rows), "known_subject_count": len(subjects),
                            "subjects_complete": all(r["subject_ref"] is not None for r in rows),
                            "numeric_count": len(values), "unknown_value_count": len(rows) - len(values),
                            "minimum": min(values) if values else None, "maximum": max(values) if values else None,
                            "limitations": sorted({r["limitations"] for r in rows})})
        delay_groups = [m for m in metrics if m["metric"] == "flight_delay_minutes" and m["unit"] == "минут"]
        known_flight_count = min_delay = max_delay = None
        if dataset["synthetic"]:
            context, name, description = "synthetic", "Синтетический пример", "Синтетическая демонстрация алгоритма."
        elif not observations:
            context = "no_observations"
            name = f"Наблюдений нет · сценариев: {len(scenario_ids)}"
            description = "Исторические последствия в этом наборе не зарегистрированы. Отсутствие наблюдений не означает отсутствие риска. Применимость сценариев проверяется через assess."
        elif (len(delay_groups) == 1 and delay_groups[0]["numeric_count"] and
              sum(m["metric"] == "flight_delay_minutes" for m in metrics) == 1):
            group = delay_groups[0]
            context = "reported_observations"
            if group["subjects_complete"]:
                known_flight_count = group["known_subject_count"]
                name = f"Рейсов: {known_flight_count} · макс. {group['maximum']:g} мин"
            else:
                name = f"Задержки: {group['observation_count']} наблюдений"
            if group["unknown_value_count"]:
                name = "Задержки рейсов · значения неполны"
            else:
                min_delay, max_delay = group["minimum"], group["maximum"]
            description = "Сводка задержек одного датированного снимка. Не итог суток; задержка рейса не равна длительности отказа системы. " + " ".join(group["limitations"])
        elif any(o["metric"] == "data_fragment_disclosed" for o in observations):
            context = "reported_observations"
            name = "Раскрытие фрагмента · объём не установлен"
            description = "Зарегистрированы сообщение о раскрытии и ответные действия. Объём раскрытия, число пострадавших и денежный ущерб не устанавливаются из количества наблюдений."
        else:
            context = "reported_observations"
            name = f"Наблюдений: {len(observations)}"
            description = "Сводка сохранённых сообщений; отдельные метрики, единицы и источники приведены в деталях. Разные снимки не объединяются в один итог."
        output.append({"id": "overview:" + process["id"], "dataset_id": dataset["dataset_id"],
                       "version": dataset["version"], "dataset_hash": dataset["sha256"],
                       "overview_version": OVERVIEW_VERSION, "derived": True, "context": context,
                       "process_id": process["id"], "process_name": process["name"],
                       "owner_id": process["owner_id"], "name": name, "description": description,
                       "observation_count": len(observations),
                       "business_effect_observation_count": sum(o["kind"] == "business_effect" for o in observations),
                       "response_observation_count": sum(o["kind"] == "response" for o in observations),
                       "scenario_count": len(scenario_ids), "source_count": len(source_ids),
                       "known_flight_count": known_flight_count,
                       "min_flight_delay_minutes": min_delay, "max_flight_delay_minutes": max_delay,
                       "scenario_ids": sorted(scenario_ids), "evidence_ids": sorted(source_ids),
                       "observation_ids": [o["id"] for o in observations], "metrics": metrics,
                       "scope_note": "Производная сводка выбранной версии набора; не новое утверждение, не оценка тяжести или вероятности. Исторические сведения не характеризуют текущее состояние систем."})
    return output


def browser_query(template, datasets, version):
    if not datasets or any(not re.fullmatch(r"[a-zA-Z0-9_-]+", d) for d in datasets):
        raise DataError("Некорректные идентификаторы наборов для запроса")
    if not re.fullmatch(r"[a-zA-Z0-9._-]+", version):
        raise DataError("Некорректная версия для запроса")
    return template.replace("$datasets", json.dumps(list(datasets), ensure_ascii=False)).replace("$version", json.dumps(version)) + "\n"


def write_browser_queries(directory, datasets, version):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "compact_overview.cypher").write_text(browser_query(COMPACT_QUERY, datasets, version), encoding="utf-8")
    (directory / "observation_details.cypher").write_text(browser_query(DETAIL_QUERY, datasets, version), encoding="utf-8")
    (directory / "overview.grass").write_text(OVERVIEW_STYLE, encoding="utf-8")


def write_overview(dataset, summaries, prefix):
    prefix = Path(prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    report = {"dataset_id": dataset["dataset_id"], "dataset_version": dataset["version"],
              "dataset_hash": dataset["sha256"], "overview_version": OVERVIEW_VERSION, "summaries": summaries}
    Path(str(prefix) + ".json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    lines = [f"# Сводка: {dataset['title']}", "", f"Набор `{dataset['dataset_id']}/{dataset['version']}`; SHA-256 `{dataset['sha256']}`.", ""]
    for s in summaries:
        lines += [f"## {s['process_name']}", "", s["name"], "", s["description"], "",
                  f"Наблюдений: {s['observation_count']}; сообщений о последствиях: {s['business_effect_observation_count']}; ответных действиях: {s['response_observation_count']}.",
                  "", s["scope_note"], ""]
        if s["metrics"]:
            lines += ["| Метрика | Единица | Сообщено | Записей | Известных объектов | Минимум | Максимум |",
                      "|---|---|---|---|---|---|---|"]
        for m in s["metrics"]:
            values = [m["metric"], m["unit"], m["reported_on"], m["observation_count"],
                      m["known_subject_count"] if m["subjects_complete"] else "не установлено", m["minimum"], m["maximum"]]
            lines.append("| " + " | ".join("не установлено" if v is None else str(v).replace("|", "\\|") for v in values) + " |")
        for m in s["metrics"]:
            lines += ["", f"Ограничения для {m['metric']} ({m['unit']}, {m['reported_on']}): " + " ".join(m["limitations"]), ""]
        sources = [e for e in dataset["entities"] if e["id"] in s["evidence_ids"]]
        lines += [f"- [{e['title']}]({e['url']}) — {e['limitations']}" for e in sources]
        lines.append("")
    Path(str(prefix) + ".md").write_text("\n".join(lines), encoding="utf-8")
    Path(str(prefix) + ".cypher").write_text(browser_query(COMPACT_QUERY, [dataset["dataset_id"]], dataset["version"]), encoding="utf-8")
