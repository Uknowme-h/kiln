import pytest
from pydantic import ValidationError

from kiln.schemas.patch import FixPatch


def _patch(**overrides):
    values = {
        "diagnosis": "the login route returns None",
        "files": [{"path": "app/accounts/routes.py", "content": "def login():\n    ...\n"}],
    }
    values.update(overrides)
    return values


def test_a_valid_patch_validates_without_context():
    patch = FixPatch.model_validate(_patch())
    assert patch.files[0].path == "app/accounts/routes.py"


def test_patch_paths_stay_inside_the_workspace():
    with pytest.raises(ValidationError):
        FixPatch.model_validate(_patch(files=[{"path": "../x.py", "content": "x"}]))


def test_duplicate_patch_files_are_rejected():
    dup = {"path": "app/a.py", "content": "x"}
    with pytest.raises(ValidationError):
        FixPatch.model_validate(_patch(files=[dup, dup]))


def test_owned_prefix_and_exact_file_ownership():
    owned = ["app/accounts/"]
    rogue = _patch(files=[{"path": "tests/test_accounts.py", "content": "x"}])
    with pytest.raises(ValidationError, match="outside the task's owned paths"):
        FixPatch.model_validate(rogue, context={"owned_paths": owned})
    ok = FixPatch.model_validate(
        _patch(files=[{"path": "app/accounts/routes.py", "content": "x"}]),
        context={"owned_paths": owned},
    )
    assert ok.files[0].path == "app/accounts/routes.py"

    exact = FixPatch.model_validate(
        _patch(files=[{"path": "models.py", "content": "x"}]),
        context={"owned_paths": ["models.py"]},
    )
    assert exact.files[0].path == "models.py"


def test_diagnosis_is_required():
    with pytest.raises(ValidationError):
        FixPatch.model_validate(_patch(diagnosis=""))
