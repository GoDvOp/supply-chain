import argparse
import json
import sys
from pathlib import Path

from .compare import compare
from .engine import assess, scenario_by_id
from .errors import DataError, InfrastructureError
from .loader import load, read_json
from .overview import write_overview
from .reports import mermaid, write_report
from .sensitivity import sensitivity_report
from .store import connect


def parser():
    p = argparse.ArgumentParser(prog="supply-risk", description="Условные последствия инцидента через поставщика")
    commands = p.add_subparsers(dest="command", required=True)
    for command in ("validate", "import"):
        commands.add_parser(command).add_argument("file", type=Path)
    for command in ("list", "assess", "compare", "sensitivity", "export", "overview"):
        sub = commands.add_parser(command)
        sub.add_argument("--dataset", required=True)
        sub.add_argument("--version", required=True)
        if command in ("assess", "compare", "sensitivity"):
            sub.add_argument("--scenario", required=True)
            sub.add_argument("--max-depth", type=int, default=5)
            sub.add_argument("--max-paths", type=int, default=100)
            sub.add_argument("--as-of")
            sub.add_argument("--output", type=Path, required=True, help="Префикс файлов .json и .md")
        if command == "assess":
            sub.add_argument("--mode", choices=("evidence", "experiment"), required=True)
            sub.add_argument("--params", type=Path)
        elif command == "compare":
            sub.add_argument("--before", type=Path, required=True)
            sub.add_argument("--after", type=Path, required=True)
        elif command == "sensitivity":
            sub.add_argument("--grid", type=Path, required=True)
        elif command == "list":
            sub.add_argument("--kind", choices=("organizations", "processes", "relations", "scenarios", "observations"), required=True)
        elif command == "export":
            sub.add_argument("--run")
            sub.add_argument("--output", type=Path, required=True)
        elif command == "overview":
            sub.add_argument("--output", type=Path)
    return p


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def run(args):
    if args.command in {"validate", "import"}:
        data = load(args.file)
        if args.command == "validate":
            return {"valid": True, "dataset_id": data["dataset_id"], "version": data["version"], "sha256": data["sha256"]}
        with connect() as store:
            return store.import_dataset(data)
    with connect() as store:
        data = store.load(args.dataset, args.version)
        if args.command == "overview":
            result = store.refresh_overview(args.dataset, args.version)
            if args.output:
                write_overview(data, result["summaries"], args.output)
            return result
        if args.command == "list":
            label = {"organizations": "Organization", "processes": "Process", "relations": "Assertion", "scenarios": "Scenario", "observations": "Observation"}[args.kind]
            return [e for e in data["entities"] if e["label"] == label]
        if args.command == "export":
            args.output.parent.mkdir(parents=True, exist_ok=True)
            if args.run:
                report = store.load_run(args.dataset, args.version, args.run)
                write_report(report, args.output)
            else:
                args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
            Path(str(args.output) + ".mmd").write_text(mermaid(data), encoding="utf-8")
            return {"exported": str(args.output)}
        scenario = scenario_by_id(data, args.scenario)
        candidates = store.candidates(data, scenario, args.max_depth, args.max_paths)
        if args.command == "assess":
            report = assess(data, args.scenario, candidates, args.mode,
                            read_json(args.params) if args.params else {}, args.as_of)
        elif args.command == "compare":
            report = compare(data, args.scenario, candidates, read_json(args.before), read_json(args.after), args.as_of)
        else:
            grid = read_json(args.grid)
            report = sensitivity_report(data, args.scenario, candidates, grid, args.as_of)
        write_report(report, args.output)
        if args.command == "compare":
            for name in ("before", "after"):
                store.save_run(report[name], str(args.output) + f".json#{name}")
        else:
            store.save_run(report, args.output)
        return {"output_json": str(args.output) + ".json", "output_markdown": str(args.output) + ".md",
                "run_id": report.get("run_id"), "scenario_id": args.scenario}


def main():
    args = parser().parse_args()
    try:
        output(run(args))
        return 0
    except (DataError, OSError) as exc:
        print(f"Ошибка данных: {exc}", file=sys.stderr)
        return 2
    except InfrastructureError as exc:
        print(f"Ошибка инфраструктуры: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
