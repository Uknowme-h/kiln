"""The reporter's bundle: README draft, run summary, and cost report.

Honest reporting is a schema rule, not a style choice: the summary must say
plainly whether the run is green, and skipped tests are never presented as
passes.
"""

from pydantic import BaseModel, Field


class ReportBundle(BaseModel):
    # pytest must not mistake the model for a test class
    __test__ = False

    readme: str = Field(min_length=1)
    run_summary: str = Field(min_length=1)
    cost_report: str = Field(min_length=1)
