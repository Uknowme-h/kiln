"""Structured test results. A run is green only when nothing failed or was skipped."""

from typing import Literal

from pydantic import BaseModel, Field


class TestCaseResult(BaseModel):
    nodeid: str = Field(min_length=1)
    outcome: Literal["passed", "failed", "skipped", "error"]
    duration_seconds: float = 0
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome == "passed"


class TestReport(BaseModel):
    cases: list[TestCaseResult] = Field(default_factory=list)
    raw_log: str = ""

    @property
    def passed(self) -> int:
        return sum(case.outcome == "passed" for case in self.cases)

    @property
    def failed(self) -> int:
        return sum(case.outcome == "failed" for case in self.cases)

    @property
    def skipped(self) -> int:
        return sum(case.outcome == "skipped" for case in self.cases)

    @property
    def errors(self) -> int:
        return sum(case.outcome == "error" for case in self.cases)

    @property
    def green(self) -> bool:
        return bool(self.cases) and self.failed == 0 and self.errors == 0 and self.skipped == 0
