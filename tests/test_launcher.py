"""Launcher contracts: preserve configuration and report failures honestly."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from conftest import ROOT


@pytest.fixture
def launcher(tmp_path):
    project = tmp_path / "project with spaces"
    project.mkdir()
    entrypoint = project / "start.sh"
    shutil.copy2(ROOT / "start.sh", entrypoint)
    config = project / ".env"
    config.write_text("NEO4J_USER=neo4j\nNEO4J_PASSWORD=test-fixture-only\n")
    config.chmod(0o600)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['LAUNCHER_CALLS'], 'a') as handle:
    handle.write(json.dumps({'args': args, 'cwd': os.getcwd()}) + '\\n')
if args == ['info'] and os.environ.get('FAIL_INFO'):
    sys.exit(1)
if args[:2] == ['compose', 'run'] and 'scripts/demo.py' in args:
    print('fixture_demo_output')
    sys.exit(int(os.environ.get('DEMO_EXIT', '0')))
if args[:2] == ['compose', 'port']:
    print('127.0.0.1:' + ('27474' if args[-1] == '7474' else '27687'))
''')
    docker.chmod(0o755)
    calls = tmp_path / "calls.jsonl"
    env = {**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"], "LAUNCHER_CALLS": str(calls)}
    return entrypoint, project, config, calls, env


def invoke(launcher, *args, **updates):
    entrypoint, _, _, _, env = launcher
    return subprocess.run([str(entrypoint), *args], cwd="/tmp", env={**env, **updates},
                          capture_output=True, text=True)


def test_one_command_preserves_config_and_reports_actual_ports(launcher):
    _, project, config, calls, _ = launcher
    original = config.read_bytes()
    result = invoke(launcher)
    assert result.returncode == 0, result.stderr
    assert "http://127.0.0.1:27474" in result.stdout
    assert "bolt://127.0.0.1:27687" in result.stdout
    assert "test-fixture-only" not in result.stdout + result.stderr
    assert config.read_bytes() == original
    records = [json.loads(line) for line in calls.read_text().splitlines()]
    assert all(record["cwd"] == str(project) for record in records)
    assert [r["args"] for r in records if r["args"][:2] == ["compose", "up"]] == [
        ["compose", "up", "-d", "--wait", "neo4j"]]
    assert (project / "outputs/startup.log").read_text() == "fixture_demo_output\n"


def test_demo_failure_propagates_exit_code_and_log(launcher):
    result = invoke(launcher, DEMO_EXIT="7")
    assert result.returncode == 7
    assert "fixture_demo_output" in result.stderr
    assert "Стенд готов" not in result.stdout


def test_unavailable_docker_is_infrastructure_failure(launcher):
    result = invoke(launcher, FAIL_INFO="1")
    assert result.returncode == 3
    assert "Docker недоступен" in result.stderr
    calls = [json.loads(line)["args"] for line in launcher[3].read_text().splitlines()]
    assert ["compose", "build", "app"] not in calls


@pytest.mark.parametrize("args,exit_code", [(["help"], 0), (["unsupported"], 2)])
def test_help_and_invalid_command_do_not_contact_docker(launcher, args, exit_code):
    result = invoke(launcher, *args)
    assert result.returncode == exit_code
    assert not launcher[3].exists()
