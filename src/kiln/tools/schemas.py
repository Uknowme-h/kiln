"""Tool inputs the coding agents will be allowed to call. Nothing here runs on the host."""

from pydantic import BaseModel, Field, field_validator

from kiln.paths import safe_relative_path


class ReadFile(BaseModel):
    path: str

    @field_validator("path")
    @classmethod
    def relative(cls, value: str) -> str:
        return safe_relative_path(value)


class WriteFile(BaseModel):
    path: str
    content: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def relative(cls, value: str) -> str:
        return safe_relative_path(value)


class ShellCommand(BaseModel):
    """A command line. The sandbox runner is the only thing that may execute one."""

    command: str = Field(min_length=1)
