"""The brief-reviewer's verdict: evidence-backed gaps against the original idea.

The suite answers "does it work"; this answers "is it what the person asked
for". A gap must carry observed evidence — the reviewer is denied opinion-only
concerns, and an empty gap list is a valid, expected verdict.
"""

from pydantic import BaseModel, Field


class ReviewGap(BaseModel):
    claim: str = Field(
        min_length=1, description="What the brief, spec, or design promised"
    )
    evidence: str = Field(
        min_length=1, description="What was observed instead, with where you saw it"
    )
    paths: list[str] = Field(
        default_factory=list, description="Files involved, when any"
    )


class ReviewVerdict(BaseModel):
    gaps: list[ReviewGap] = Field(default_factory=list)
    summary: str = Field(min_length=1, description="One line: clear, or the gap count")
