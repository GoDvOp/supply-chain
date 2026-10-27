import json
from pathlib import Path

from .errors import DataError

STATUS = {"conditional": "Условный путь", "blocked": "Путь заблокирован",
          "applicable_in_scenario": "Применим при заданном инциденте"}


def display(value):
    if isinstance(value, float):
        return f"{value:.6g}"
    if value is None:
        return "неизвестно / не применимо"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).replace("|", "\\|").replace("\n", " ")
    return str(value).replace("|", "\\|").replace("\n", " ")


def assessment_markdown(r):
    lines = [f"# {r['incident']['name']}", "", r["incident"]["description"], "",
             f"Набор: `{r['dataset_id']}/{r['dataset_version']}`; режим: `{r['mode']}`; правила: `{r['rules_version']}`.",
             f"Расчёт: `{r['run_id']}`; дата: {r['created_at']}; актуальность оценивается на {r['as_of']}.",
             f"SHA-256 набора: `{r['dataset_hash']}`; входа: `{r['input_hash']}`.", "", "## Факты и границы утверждений", ""]
    if r.get("analysis_context") == "historical_reconstruction":
        date_label = "дата публичного сообщения" if r["incident"].get("incident_date_basis") == "reported_on" else "дата события"
        lines[2:2] = [f"Историческая реконструкция: {date_label} {r['incident']['incident_date']}. "
                      "Наблюдения из источников сохраняются и при изменении экспериментальных условий.", ""]
    for f in r["facts"]:
        lines.append(f"- `{f['id']}` ({f['status']}): {f['scope_note']} Источники: {', '.join(f['evidence_ids']) or 'синтетика'}.")
    lines += ["", "## Допущения и переопределения", ""]
    for o in r["overrides"]:
        lines.append(f"- `{o.get('condition_id', o.get('parameter_id'))}`: {display(o['old_value'])} → {display(o['new_value'])}; {o['origin']}: {o['explanation']}")
    if not r["overrides"]:
        lines.append("Переопределений нет. Неизвестные значения сохранены.")
    lines += ["", "## Пути и блокировки", ""]
    for p in r["paths"]:
        lines += [f"### {STATUS[p['status']]}", "", " → ".join(n["name"] for n in p["nodes"]), ""]
        for t in p["transitions"]:
            lines.append(f"- `{t['type']}` ({t['direction']}), утверждение `{t['assertion_id']}`, правила: {', '.join(t['rule_ids'])}. {t['scope_note']}")
        for c in p["conditions"]:
            lines.append(f"- Условие `{c['id']}`: {display(c['value'])}; происхождение: {c['origin']}. {c['description']}")
        if p["blocking_conditions"]:
            lines.append(f"Блокируют: {', '.join(p['blocking_conditions'])}.")
        lines += [f"Источники пути: {', '.join(p['evidence_ids']) or 'синтетический пример'}."]
        lines += [f"Ограничение: {note}" for note in p["limitations"]]
    if not r["paths"]:
        lines.append("Путь не найден в данном наборе")
    if r.get("observations"):
        lines += ["", "## Наблюдаемые последствия и ответные действия", "",
                  "Далее приведены сведения из опубликованных сообщений. Это отдельный реестр наблюдений, а не результат вычисления модели или оценка денежных потерь.", "",
                  "| Сообщено | Вид | Объект | Наблюдение | Значение | Основание | Источники | Ограничения |",
                  "|---|---|---|---|---|---|---|---|"]
        for o in r["observations"]:
            values = [o["reported_on"], o["kind"], o["subject_ref"], o["description"],
                      f"{display(o['value'])} {o['unit']}" if o["value"] is not None else "Число не установлено",
                      o["basis"], ", ".join(o["evidence_ids"]), o["limitations"]]
            lines.append("| " + " | ".join(display(v) for v in values) + " |")
    lines += ["", "## Последствия и расчёты", ""]
    if r.get("calculation_model", "edi_queue") == "edi_queue":
        lines += ["Q = max(0, lambda − mu_b) × T; при Q > 0 и mu_r > lambda: T_clear = Q / (mu_r − lambda); total_time = T + T_clear.", ""]
    for p in r["affected_processes"]:
        lines += [f"### {p['name']}", "", f"Путей: {p['path_count']}. {STATUS[p['status']]}. Категория последствий: {p['severity']}; условная категория: {p['potential_severity']}.",
                  p["calculation_scope"], "", "| Показатель | Значение |", "|---|---|"]
        for k, v in sorted(r["calculations"][p["process_id"]].items()):
            lines.append(f"| {k} | {display(v)} |")
        if p["dependency_evaluation"]:
            lines += ["", f"Резервирование: {display(p['dependency_evaluation'])}."]
    lines += ["", "## Неизвестные условия и приоритет уточнения", ""]
    for u in r["unknowns"]:
        lines.append(f"- `{u['id']}`: {u['description']}")
    for p in r["clarification_priorities"]:
        lines.append(f"- `{p['condition_id']}`: {p['priority'] or 'не определён'}. {p['reason']}. Источник для уточнения: {p['public_source_hint']}")
        for key, label in (("true_result", "true"), ("false_result", "false")):
            value = p[key]
            lines.append(f"  Эксперимент {label}: пути {display(value['paths'])}; процессы {display(value['processes'])}; расчёты {display(value['calculations'])}.")
    lines += ["", "## Источники", ""]
    for s in r["sources"]:
        lines.append(f"- `{s['id']}`: [{s['title']}]({s['url']}); {s['publisher']}; публикация: {s['published_at'] or 'неизвестна'}; проверено: {s['accessed_at']}. {s['limitations']}")
    lines += ["", "## Ограничения", "", f"Поиск: {display(r['search'])}."]
    lines += [f"- {v}" for v in r["limitations"]]
    return "\n".join(lines) + "\n"


def markdown(report):
    if "before" in report:
        lines = ["# Сравнение мер", "", "| Поле | До | После |", "|---|---|---|"]
        for d in report["changes"] + report["outcome_changes"]:
            lines.append(f"| {display(d['field'])} | {display(d['before'])} | {display(d['after'])} |")
        return "\n".join(lines) + "\n\n" + assessment_markdown(report["before"]) + "\n" + assessment_markdown(report["after"])
    if "rows" in report:
        lines = ["# Анализ чувствительности", "", "Эксперимент; проектная гипотеза.", ""]
        if "incident" in report:
            lines += [report["grid_scope"], ""]
            context = assessment_markdown(report)
            lines += ["\n".join("#" + line if line.startswith("#") else line for line in context.splitlines()), ""]
        lines += ["## Комбинации параметров", ""]
        if report["rows"]:
            lines += [f"Фиксированное lambda: {display(report['rows'][0]['parameters']['lambda'])} сообщений/ч. "
                      f"Порог и его происхождение: {display(report['rows'][0]['threshold'])}.", ""]
        lines += ["| T | mu_b | mu_r | Q | T_clear | total_time | статус | последствия | Превышен порог |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for r in report["rows"]:
            vals = [r["parameters"]["T"], r["parameters"]["mu_b"], r["parameters"]["mu_r"],
                    r["Q"], r["T_clear"], r["total_time"], r["status"], r["severity"], r["threshold_exceeded"]]
            lines.append("| " + " | ".join(display(v) for v in vals) + " |")
        lines += ["", f"Конечные результаты: минимум {report['finite_min']}, максимум {report['finite_max']}, среднее {report['finite_mean']} ч.",
                  f"non_draining: {report['non_draining_count']}; исключены из конечных агрегатов.", report["conclusion_changes_when"]]
        return "\n".join(lines) + "\n"
    return assessment_markdown(report)


def write_report(report, prefix):
    prefix = Path(prefix)
    try:
        prefix.parent.mkdir(parents=True, exist_ok=True)
        Path(str(prefix) + ".json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        Path(str(prefix) + ".md").write_text(markdown(report), encoding="utf-8")
    except OSError as exc:
        raise DataError(f"Не удалось сохранить отчёт: {exc}") from exc


def mermaid(dataset):
    entities = [e for e in dataset["entities"] if e["label"] in {"Organization", "Service", "Process", "DataCategory"}]
    aliases = {e["id"]: f"n{i}" for i, e in enumerate(entities)}
    def escape(text):
        return text.replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;').replace('>', '&gt;').replace('\n', ' ')
    lines = ["flowchart LR"]
    for e in entities:
        lines.append(f'  {aliases[e["id"]]}["{escape(e["name"])} ({e["label"]})"]')
    for e in dataset["entities"]:
        if e["label"] == "Assertion":
            lines.append(f'  {aliases[e["subject_id"]]} -->|"{e["predicate"]}: {escape(e["id"])}"| {aliases[e["object_id"]]}')
    return "\n".join(lines) + "\n"
