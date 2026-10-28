"""Shared Pydantic models for agent inputs and outputs."""

from kiln.schemas.patch import FixPatch, PatchFile
from kiln.schemas.report import ReportBundle
from kiln.schemas.results import TestCaseResult, TestReport
from kiln.schemas.spec import AcceptanceCriterion, FilePlanEntry, Spec, UserStory
from kiln.schemas.suite import TestFile, TestSuite
from kiln.schemas.tasks import Task, TaskGraph
from kiln.schemas.work import WorkerResult, WorkFile

__all__ = [
    "AcceptanceCriterion",
    "FilePlanEntry",
    "FixPatch",
    "PatchFile",
    "ReportBundle",
    "Spec",
    "Task",
    "TaskGraph",
    "TestCaseResult",
    "TestFile",
    "TestReport",
    "TestSuite",
    "UserStory",
    "WorkFile",
    "WorkerResult",
]
