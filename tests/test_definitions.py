from kiln.agents.definitions import DEFINITIONS, definition_for, may_write
from kiln.agents.roles import Role


def test_every_role_has_a_definition():
    assert set(DEFINITIONS) == set(Role)
    for role in Role:
        assert definition_for(role).role is role
        assert definition_for(role).label
        assert definition_for(role).prompt


def test_only_build_roles_get_file_tools():
    coding = {Role.TEST_AUTHOR, Role.WORKER, Role.FIXER, Role.REPORTER}
    for role in Role:
        tools = definition_for(role).tools
        if role in coding:
            assert "read_file" in tools
        else:
            assert tools == ()


def test_test_files_belong_to_the_test_author_alone():
    assert may_write(Role.TEST_AUTHOR, "tests/test_app.py")
    assert not may_write(Role.WORKER, "tests/test_app.py")
    assert not may_write(Role.FIXER, "tests/test_app.py", owned_paths=("app/",))
    assert may_write(Role.WORKER, "app/main.py", owned_paths=("app/",))
    assert not may_write(Role.PLANNER, "app/main.py")


def test_owned_paths_never_widen_a_role_past_a_prefix():
    assert not may_write(Role.WORKER, "secret.txt")
    assert not may_write(Role.WORKER, "appx/main.py", owned_paths=("app/",))
