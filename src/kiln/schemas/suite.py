"""Test suite produced by the test author, from the acceptance criteria.

The keystone of the independent test gate: every file declares which
acceptance criteria it covers, and validation — given the spec's criterion
ids via pydantic context — requires complete, honest coverage. Workers never
see these files as writable.
"""

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from kiln.paths import safe_relative_path


class TestFile(BaseModel):
    path: str = Field(min_length=1)
    content: str = Field(min_length=1)
    covers: list[str] = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def inside_the_test_tree(cls, value: str) -> str:
        path = safe_relative_path(value)
        if not path.startswith("tests/") or not path.endswith(".py"):
            raise ValueError(f"test files live under tests/ and end in .py: {value}")
        return path


class TestSuite(BaseModel):
    # pytest must not mistake the model for a test class
    __test__ = False

    spec_title: str = Field(min_length=1)
    files: list[TestFile] = Field(min_length=1)

    @model_validator(mode="after")
    def coverage_is_complete_and_honest(self, info: ValidationInfo) -> "TestSuite":
        paths = [file.path for file in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("test file paths must be unique")

        criterion_ids = (info.context or {}).get("criterion_ids")
        if criterion_ids is not None:
            known = set(criterion_ids)
            covered: set[str] = set()
            for file in self.files:
                for criterion in file.covers:
                    if criterion not in known:
                        raise ValueError(f"{file.path} covers unknown criterion {criterion}")
                    covered.add(criterion)
            missing = known - covered
            if missing:
                raise ValueError(f"no test covers: {', '.join(sorted(missing))}")
        return self
