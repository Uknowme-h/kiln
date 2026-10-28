"""A patch from the fixer: full corrected file contents inside owned paths.

Whole files, not diffs: workers own small files, structured output gives us
clean full contents, and there is no fragile diff application to get wrong.
The patch may only touch paths the failing task owns — enforced here when the
owned paths are supplied via pydantic context.
"""

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from kiln.paths import safe_relative_path


class PatchFile(BaseModel):
    path: str = Field(min_length=1)
    content: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def workspace_relative(cls, value: str) -> str:
        return safe_relative_path(value)


class FixPatch(BaseModel):
    # pytest must not mistake the model for a test class
    __test__ = False

    diagnosis: str = Field(min_length=1)
    files: list[PatchFile] = Field(min_length=1)

    @model_validator(mode="after")
    def only_owned_paths(self, info: ValidationInfo) -> "FixPatch":
        paths = [file.path for file in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("patch file paths must be unique")
        owned = (info.context or {}).get("owned_paths")
        if owned is not None:
            prefixes = tuple(owned)
            for path in paths:
                if not any(path == p or path.startswith(p.rstrip("/") + "/") for p in prefixes):
                    raise ValueError(f"patch touches {path}, outside the task's owned paths")
        return self
