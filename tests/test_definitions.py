from kiln.agents.definitions import DEFINITIONS, definition_for, may_write, system_prompt_for
from kiln.agents.roles import Role
from kiln.agents.skills import (
    PROD_OPS_ROLES,
    SECURITY_ROLES,
    UI_PRODUCT_ROLES,
    handle_skill_tool,
    load_skill,
    needs_prod_ops_skill,
    needs_security_skill,
    needs_ui_product_skill,
)
from kiln.permissions import evaluate
from kiln.schemas.spec import AcceptanceCriterion, FilePlanEntry, Spec, UserStory
from kiln.tools.registry import TOOLS


def test_every_role_has_a_definition():
    assert set(DEFINITIONS) == set(Role)
    for role in Role:
        assert definition_for(role).role is role
        assert definition_for(role).label
        assert definition_for(role).prompt
        assert definition_for(role).output_guidance, f"{role} needs output guidance"


def test_the_system_prompt_appends_the_output_guidance():
    # every role's soft cap rides along in its system prompt, after the template
    for role in Role:
        prompt = system_prompt_for(role)
        guidance = definition_for(role).output_guidance
        assert guidance in prompt
        assert prompt.index(guidance) > 0
        assert len(prompt) > len(guidance)


def test_ui_product_skill_off_by_default():
    skill = load_skill("ui_product")
    assert "Integration done-definition" in skill
    assert "served UI" in skill
    for role in Role:
        assert skill not in system_prompt_for(role)


def test_ui_product_skill_appends_only_when_flagged_for_product_roles():
    skill = load_skill("ui_product")
    for role in Role:
        prompt = system_prompt_for(role, ui_product=True)
        if role.value in UI_PRODUCT_ROLES:
            assert skill in prompt
            assert prompt.index(definition_for(role).output_guidance) < prompt.index(skill)
        else:
            assert skill not in prompt


def _minimal_spec(**kwargs) -> Spec:
    defaults = dict(
        title="Demo",
        summary="a small demo",
        stack="FastAPI",
        stack_id="python",
        user_stories=[
            UserStory(id="s1", as_a="user", i_want="an API", so_that="I can call it"),
        ],
        acceptance_criteria=[
            AcceptanceCriterion(
                id="ac1",
                story_id="s1",
                given="the server is up",
                when="GET /health",
                then="status is ok",
                oracle="tests/test_health.py asserts 200",
            ),
        ],
        file_plan=[FilePlanEntry(path="app/main.py", purpose="entry")],
    )
    defaults.update(kwargs)
    return Spec(**defaults)


def test_needs_ui_product_skill_from_idea_hints():
    assert needs_ui_product_skill(idea="build a login page with clean UI")
    assert needs_ui_product_skill(idea="embed Excalidraw whiteboard")
    assert not needs_ui_product_skill(idea="REST JSON API for invoices only")


def test_needs_ui_product_skill_from_discovery_ui_direction():
    assert needs_ui_product_skill(
        discovery="## UI direction\nWarm stone palette, serif display, full-bleed hero.\n"
    )
    assert not needs_ui_product_skill(discovery="## UI direction\nN/A\n")
    assert not needs_ui_product_skill(discovery="## Auth model\nreal sessions\n")


def test_needs_ui_product_skill_from_spec_paths_and_stories():
    assert needs_ui_product_skill(
        spec=_minimal_spec(
            file_plan=[
                FilePlanEntry(path="public/index.html", purpose="landing"),
                FilePlanEntry(path="app/main.py", purpose="server"),
            ]
        )
    )
    assert needs_ui_product_skill(
        spec=_minimal_spec(
            user_stories=[
                UserStory(
                    id="s1",
                    as_a="visitor",
                    i_want="a dashboard UI",
                    so_that="I see status",
                ),
            ]
        )
    )
    assert not needs_ui_product_skill(spec=_minimal_spec())


def test_load_skill_rejects_bad_names():
    try:
        load_skill("../secrets")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for non-identifier skill name")


def test_prod_ops_and_security_skills_gate_by_role_and_flag():
    prod = load_skill("prod_ops")
    sec = load_skill("security")
    assert ".env.example" in prod
    assert "HttpOnly" in sec or "httponly" in sec.lower() or "password" in sec.lower()
    for role in Role:
        bare = system_prompt_for(role)
        assert prod not in bare and sec not in bare
        flagged = system_prompt_for(role, prod_ops=True, security=True)
        if role.value in PROD_OPS_ROLES:
            assert prod in flagged
        else:
            assert prod not in flagged
        if role.value in SECURITY_ROLES:
            assert sec in flagged
        else:
            assert sec not in flagged


def test_needs_security_and_prod_ops_from_idea():
    assert needs_security_skill(idea="signup and login with accounts")
    assert needs_prod_ops_skill(idea="signup and login with accounts")
    assert needs_prod_ops_skill(idea="a clean UI landing page")
    assert not needs_security_skill(idea="add two numbers in a CLI")


def test_handle_skill_tool_loads_catalog_entries():
    text = handle_skill_tool({"name": "ui_product"})
    assert "Integration done-definition" in text
    bad = handle_skill_tool({"name": "nope"})
    assert "unknown skill" in bad.lower() or "available" in bad.lower()


def test_no_role_pins_a_hard_output_budget():
    # length is guided in the prompt; the only ceiling is the global safety net
    for role in Role:
        assert definition_for(role).max_output_tokens is None


def test_every_declared_tool_exists_in_the_catalogue():
    for role in Role:
        for tool in definition_for(role).tools:
            assert tool in TOOLS, f"{role} lists unknown tool {tool}"


def test_only_build_roles_write_files_and_only_devs_and_fixer_shell():
    # requirements writes docs/discovery.md; the planner writes its plan note
    writers = {Role.REQUIREMENTS, Role.TEST_AUTHOR, Role.WORKER, Role.FIXER, Role.PLANNER}
    shellers = {Role.WORKER, Role.FIXER}
    for role in Role:
        tools = definition_for(role).tools
        assert ("write_file" in tools) == (role in writers)
        assert ("shell" in tools) == (role in shellers)


def test_only_requirements_and_planner_ask_and_only_requirements_and_devs_browse():
    for role in Role:
        tools = definition_for(role).tools
        assert ("ask_user" in tools) == (role in {Role.REQUIREMENTS, Role.PLANNER})
        assert ("web_fetch" in tools) == (role in {Role.REQUIREMENTS, Role.WORKER})


def test_the_reporter_reads_and_verifies_but_never_writes_or_shells():
    tools = definition_for(Role.REPORTER).tools
    assert {"read_file", "glob", "grep", "submit_result"} <= set(tools)
    assert "write_file" not in tools and "shell" not in tools


def test_role_permissions_match_the_tool_lists():
    worker = definition_for(Role.WORKER).permissions
    assert evaluate("shell", "pytest -q", worker).effect == "ask"
    assert evaluate("edit", "tests/test_x.py", worker).effect == "deny"
    assert evaluate("edit", "app/main.py", worker).effect == "allow"
    reporter = definition_for(Role.REPORTER).permissions
    assert evaluate("shell", "ls", reporter).effect == "deny"
    assert evaluate("edit", "app/main.py", reporter).effect == "deny"
    assert evaluate("edit", "README.md", reporter).effect == "allow"
    fixer = definition_for(Role.FIXER).permissions
    assert evaluate("web_fetch", "https://x.dev", fixer).effect == "deny"


def test_the_planner_scope_covers_only_the_plan_documents():
    planner_rules = definition_for(Role.PLANNER).permissions
    # both plan documents are writable: the model writes the plan note, and the
    # orchestrator renders the contract under this same scope
    assert evaluate("edit", "docs/plan.md", planner_rules).effect == "allow"
    assert evaluate("edit", "docs/contract.md", planner_rules).effect == "allow"
    # nothing else in docs/, nothing outside docs/, and no shell
    assert evaluate("edit", "docs/other.md", planner_rules).effect == "deny"
    assert evaluate("edit", "app/main.py", planner_rules).effect == "deny"
    assert evaluate("shell", "ls", planner_rules).effect == "deny"
    # the plan-approval beat is always asked, for every role
    assert evaluate("plan", "task-graph", ()).effect == "ask"


def test_test_files_belong_to_the_test_author_alone():
    assert may_write(Role.TEST_AUTHOR, "tests/test_app.py")
    assert may_write(Role.TEST_AUTHOR, "docs/browser-flows.json")
    assert not may_write(Role.WORKER, "tests/test_app.py")
    assert not may_write(Role.FIXER, "tests/test_app.py", owned_paths=("app/",))
    assert may_write(Role.WORKER, "app/main.py", owned_paths=("app/",))
    assert not may_write(Role.PLANNER, "app/main.py")
    tessa = definition_for(Role.TEST_AUTHOR).permissions
    assert evaluate("edit", "docs/browser-flows.json", tessa).effect == "allow"
    assert evaluate("edit", "docs/other.md", tessa).effect == "deny"


def test_owned_paths_never_widen_a_role_past_a_prefix():
    assert not may_write(Role.WORKER, "secret.txt")
    assert not may_write(Role.WORKER, "appx/main.py", owned_paths=("app/",))
