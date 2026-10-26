"""Turn pytest JUnit XML into a TestReport with trimmed messages."""

import xml.etree.ElementTree as ET
from pathlib import Path

from kiln.schemas.results import TestCaseResult, TestReport

_LOG_LIMIT = 2000


def parse_junit(source: str | Path) -> TestReport:
    if isinstance(source, Path):
        text = source.read_text(encoding="utf-8")
    else:
        text = source
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError("junit xml is not valid") from exc

    cases: list[TestCaseResult] = []
    for testcase in root.iter("testcase"):
        classname = testcase.attrib.get("classname", "")
        name = testcase.attrib.get("name", "")
        nodeid = f"{classname}::{name}" if classname else name
        if not nodeid:
            raise ValueError("testcase is missing a name")
        duration = float(testcase.attrib.get("time", "0") or 0)
        failure = testcase.find("failure")
        error = testcase.find("error")
        skipped = testcase.find("skipped")
        if failure is not None:
            outcome = "failed"
            message = _detail(failure)
        elif error is not None:
            outcome = "error"
            message = _detail(error)
        elif skipped is not None:
            outcome = "skipped"
            message = _detail(skipped)
        else:
            outcome = "passed"
            message = ""
        cases.append(
            TestCaseResult(
                nodeid=nodeid,
                outcome=outcome,
                duration_seconds=duration,
                message=message,
            )
        )
    return TestReport(cases=cases, raw_log=_trim(text))


def _detail(element: ET.Element) -> str:
    message = element.attrib.get("message", "")
    body = element.text or ""
    return _trim("\n".join(part for part in (message, body) if part))


def _trim(text: str) -> str:
    text = text.strip()
    if len(text) <= _LOG_LIMIT:
        return text
    return text[:_LOG_LIMIT] + "\n...[trimmed]"
