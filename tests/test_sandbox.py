from pathlib import Path

import pytest

from kiln.config import Settings
from kiln.sandbox.parser import parse_junit
from kiln.sandbox.runner import build_docker_args

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" tests="3" failures="1" skipped="1" errors="0">
    <testcase classname="tests.test_app" name="test_ok" time="0.01" />
    <testcase classname="tests.test_app" name="test_bad" time="0.20">
      <failure message="AssertionError: assert 1 == 2">trace line</failure>
    </testcase>
    <testcase classname="tests.test_app" name="test_later" time="0">
      <skipped message="not now" />
    </testcase>
  </testsuite>
</testsuites>
"""


def test_parser_counts_and_refuses_to_call_a_partial_run_green():
    report = parse_junit(JUNIT)
    assert report.passed == 1
    assert report.failed == 1
    assert report.skipped == 1
    assert report.green is False
    assert "AssertionError" in report.cases[1].message


def test_an_all_passing_report_is_green():
    xml = """<testsuite>
      <testcase classname="tests.test_app" name="test_ok" time="0.01" />
    </testsuite>"""
    assert parse_junit(xml).green is True


def test_invalid_xml_is_rejected():
    with pytest.raises(ValueError):
        parse_junit("<nope")


def test_docker_args_block_network_and_do_not_pass_secrets():
    settings = Settings(
        _env_file=None,
        model_api_key="spark-key",
        sandbox_image="kiln-sandbox:latest",
        sandbox_memory="512m",
        sandbox_cpus="1.0",
    )
    args = build_docker_args(Path("."), settings, "kiln-test")
    assert args[0] == "docker"
    assert "none" in args
    assert "--read-only" in args
    assert "512m" in args
    assert "spark-key" not in args
    assert not any(arg.startswith("--env") for arg in args)
