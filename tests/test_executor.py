import pytest

from kiln.companion.executor import ExecutionError, Executor


@pytest.fixture()
def executor(tmp_path):
    return Executor(tmp_path)


def test_write_then_read_round_trips_inside_the_workspace(executor):
    result = executor.execute("write_file", {"path": "app/main.py", "content": "print(1)\n"})
    assert "wrote app/main.py" in result
    assert executor.execute("read_file", {"path": "app/main.py"}) == "print(1)\n"


def test_write_creates_missing_parent_folders(executor, tmp_path):
    executor.execute("write_file", {"path": "deep/nested/file.txt", "content": "x"})
    assert (tmp_path / "deep" / "nested" / "file.txt").is_file()


def test_paths_cannot_escape_the_workspace(executor, tmp_path):
    with pytest.raises(Exception):
        executor.execute("write_file", {"path": "../outside.txt", "content": "x"})
    with pytest.raises(Exception):
        executor.execute("read_file", {"path": "../outside.txt"})
    with pytest.raises(Exception):
        executor.execute("write_file", {"path": "/abs/path.txt", "content": "x"})
    assert not (tmp_path.parent / "outside.txt").exists()


def test_reading_a_missing_file_is_an_error(executor):
    with pytest.raises(ExecutionError):
        executor.execute("read_file", {"path": "nope.txt"})


def test_an_unknown_tool_is_an_error(executor):
    with pytest.raises(ExecutionError):
        executor.execute("shell_command", {"command": "rm -rf /"})


def test_empty_content_is_rejected_by_the_schema(executor):
    with pytest.raises(Exception):
        executor.execute("write_file", {"path": "a.txt", "content": ""})


def test_workspace_must_exist(tmp_path):
    with pytest.raises(ExecutionError):
        Executor(tmp_path / "missing")
