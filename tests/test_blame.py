from kiln.graph.orchestrator import blame_tasks
from kiln.schemas.results import TestCaseResult as CaseResult
from kiln.schemas.results import TestReport as Report
from kiln.schemas.suite import TestFile as SuiteFile
from kiln.schemas.suite import TestSuite as Suite
from kiln.schemas.tasks import Task, TaskGraph


def test_blame_uses_suite_covers_to_find_the_owning_task():
    graph = TaskGraph(
        spec_title="X",
        tasks=[
            Task(
                id="scaffold",
                title="Scaffold",
                description="shared",
                owned_paths=["app/__init__.py"],
                kind="scaffold",
            ),
            Task(
                id="accounts",
                title="Accounts",
                description="auth",
                owned_paths=["app/accounts.py"],
                depends_on=["scaffold"],
                covers=["c1"],
            ),
            Task(
                id="todos",
                title="Todos",
                description="items",
                owned_paths=["app/todos.py"],
                depends_on=["scaffold"],
                covers=["c2"],
            ),
        ],
    )
    suite = Suite(
        spec_title="X",
        files=[
            SuiteFile(
                path="tests/test_accounts.py",
                content="def test_x():\n    assert True\n",
                covers=["c1"],
            ),
            SuiteFile(
                path="tests/test_todos.py",
                content="def test_y():\n    assert True\n",
                covers=["c2"],
            ),
        ],
    )
    report = Report(
        cases=[
            CaseResult(
                nodeid="tests/test_accounts.py::test_x",
                outcome="failed",
                message="boom",
            )
        ],
    )
    owners = blame_tasks(report, graph, suite)
    assert [t.id for t in owners] == ["accounts"]


def test_blame_understands_pytest_dotted_junit_classnames():
    """pytest's JUnit writes `tests.test_accounts::test_x` (and
    `tests.test_accounts.TestLogin::test_x` for a class). Only path-style ids
    used to match, so every pytest failure fell through to the scaffolds and
    the fixer patched the wrong files (e2e simulator, fix_loop scenario)."""
    graph = TaskGraph(
        spec_title="X",
        tasks=[
            Task(id="scaffold", title="S", description="s", owned_paths=["app/__init__.py"], kind="scaffold"),
            Task(id="accounts", title="A", description="a", owned_paths=["app/accounts.py"],
                 depends_on=["scaffold"], covers=["c1"]),
            Task(id="accounts_v2", title="A2", description="a2", owned_paths=["app/accounts_v2.py"],
                 depends_on=["scaffold"], covers=["c2"]),
        ],
    )
    suite = Suite(
        spec_title="X",
        files=[
            SuiteFile(path="tests/test_accounts.py", content="def test_x():\n    pass\n", covers=["c1"]),
            SuiteFile(path="tests/test_accounts_v2.py", content="def test_y():\n    pass\n", covers=["c2"]),
        ],
    )
    for nodeid in ("tests.test_accounts::test_x", "tests.test_accounts.TestLogin::test_x"):
        report = Report(cases=[CaseResult(nodeid=nodeid, outcome="failed", message="boom")])
        assert [t.id for t in blame_tasks(report, graph, suite)] == ["accounts"], nodeid


def test_blame_falls_back_to_the_scaffold_not_the_first_task():
    graph = TaskGraph(
        spec_title="X",
        tasks=[
            Task(
                id="feature",
                title="Feature",
                description="f",
                owned_paths=["app/f.py"],
            ),
            Task(
                id="scaffold",
                title="Scaffold",
                description="shared",
                owned_paths=["app/__init__.py"],
                kind="scaffold",
            ),
        ],
    )
    suite = Suite(
        spec_title="X",
        files=[
            SuiteFile(
                path="tests/test_other.py",
                content="def test_z():\n    assert True\n",
                covers=["c9"],  # a criterion no task claims
            )
        ],
    )
    report = Report(
        cases=[
            CaseResult(
                nodeid="tests/test_missing.py::test_x",
                outcome="failed",
                message="boom",
            )
        ]
    )
    owners = blame_tasks(report, graph, suite)
    assert [t.id for t in owners] == ["scaffold"]  # shared wiring, not feature


def test_blame_without_a_scaffold_widens_to_every_task():
    graph = TaskGraph(
        spec_title="X",
        tasks=[
            Task(id="t1", title="One", description="a", owned_paths=["a.py"]),
            Task(id="t2", title="Two", description="b", owned_paths=["b.py"]),
        ],
    )
    report = Report(
        cases=[
            CaseResult(
                nodeid="tests/test_unmapped.py::test_x",
                outcome="failed",
                message="boom",
            )
        ]
    )
    owners = blame_tasks(report, graph, None)
    assert {task.id for task in owners} == {"t1", "t2"}


def test_blame_when_nothing_failed_uses_the_same_honest_fallback():
    graph = TaskGraph(
        spec_title="X",
        tasks=[
            Task(
                id="scaffold",
                title="Scaffold",
                description="shared",
                owned_paths=["app/__init__.py"],
                kind="scaffold",
            ),
            Task(id="t1", title="One", description="a", owned_paths=["a.py"]),
        ],
    )
    # not green (a skip) but no failed/error case to attribute
    report = Report(cases=[CaseResult(nodeid="t1", outcome="skipped")])
    owners = blame_tasks(report, graph, None)
    assert [task.id for task in owners] == ["scaffold"]
