import pytest
from pydantic import ValidationError

from kiln.schemas.suite import TestSuite


def _suite(**overrides):
    values = {
        "spec_title": "Tasks",
        "files": [
            {
                "path": "tests/test_tasks.py",
                "content": "def test_add():\n    assert True\n",
                "covers": ["c1"],
            }
        ],
    }
    values.update(overrides)
    return values


def test_a_valid_suite_validates():
    suite = TestSuite.model_validate(_suite(), context={"criterion_ids": ["c1"]})
    assert suite.files[0].path == "tests/test_tasks.py"


def test_test_files_must_live_under_the_test_tree():
    bad = [
        "app/test_x.py",  # outside tests/
        "tests/x.txt",  # not python
        "../tests/x.py",  # escapes the workspace
    ]
    for path in bad:
        with pytest.raises(ValidationError):
            bad_file = {"path": path, "content": "x", "covers": ["c1"]}
            TestSuite.model_validate(_suite(files=[bad_file]))


def test_file_paths_must_be_unique():
    dup = {"path": "tests/test_a.py", "content": "x", "covers": ["c1"]}
    with pytest.raises(ValidationError):
        TestSuite.model_validate(_suite(files=[dup, dup]))


def test_covers_must_name_real_criteria():
    with pytest.raises(ValidationError, match="unknown criterion"):
        TestSuite.model_validate(_suite(), context={"criterion_ids": ["other"]})


def test_every_criterion_needs_at_least_one_test():
    with pytest.raises(ValidationError, match="no test covers"):
        TestSuite.model_validate(_suite(), context={"criterion_ids": ["c1", "c2"]})


def test_coverage_context_is_optional_for_structural_validation():
    suite = TestSuite.model_validate(_suite())
    assert suite.spec_title == "Tasks"
