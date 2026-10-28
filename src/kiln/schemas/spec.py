"""Typed spec produced by the requirements analyst."""

from pydantic import BaseModel, Field, model_validator


class UserStory(BaseModel):
    id: str = Field(min_length=1)
    as_a: str = Field(min_length=1)
    i_want: str = Field(min_length=1)
    so_that: str = Field(min_length=1)


class AcceptanceCriterion(BaseModel):
    id: str = Field(min_length=1)
    story_id: str = Field(min_length=1)
    given: str = Field(min_length=1)
    when: str = Field(min_length=1)
    then: str = Field(min_length=1)
    oracle: str = Field(
        min_length=1,
        description=(
            "The exact automated check that proves this criterion, "
            "e.g. the test file and what it asserts."
        ),
    )


class FilePlanEntry(BaseModel):
    path: str = Field(min_length=1)
    purpose: str = Field(min_length=1)


class Spec(BaseModel):
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    stack: str = Field(min_length=1)
    user_stories: list[UserStory] = Field(min_length=1)
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1)
    file_plan: list[FilePlanEntry] = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def story_references_resolve(self) -> "Spec":
        story_ids = [story.id for story in self.user_stories]
        if len(story_ids) != len(set(story_ids)):
            raise ValueError("user story ids must be unique")
        known = set(story_ids)
        criterion_ids = [item.id for item in self.acceptance_criteria]
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("acceptance criterion ids must be unique")
        for criterion in self.acceptance_criteria:
            if criterion.story_id not in known:
                raise ValueError(f"unknown story_id {criterion.story_id}")
        return self
