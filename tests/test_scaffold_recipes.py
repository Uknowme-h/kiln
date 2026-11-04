import pytest

from kiln.stacks.scaffold_recipes import (
    RECIPES,
    recipe_for,
    scaffold_argv,
    scaffold_files,
)


def test_every_framework_id_has_a_recipe_except_deferred_django():
    from kiln.schemas.spec import FRAMEWORK_IDS

    for framework in FRAMEWORK_IDS:
        if framework == "django":  # deferred: no Kiln test profile yet
            continue
        assert framework in RECIPES


def test_recipe_for_unknown_framework_raises():
    with pytest.raises(ValueError, match="no scaffold recipe"):
        recipe_for("rails")


def test_argv_recipes_substitute_the_root_and_pin_a_version():
    argv = scaffold_argv("vite_react", "frontend", {})
    assert argv[:3] == ["npm", "create", "vite@6"]
    assert "frontend" in argv
    assert "--template" in argv and "react-ts" in argv

    argv = scaffold_argv("nextjs", "web", {})
    assert any("create-next-app@15" in part for part in argv)
    assert "--disable-git" in argv


def test_argv_recipe_rejects_template_frameworks_and_unknown_options():
    with pytest.raises(ValueError, match="template recipe"):
        scaffold_argv("fastapi", "backend", {})
    with pytest.raises(ValueError, match="unsupported scaffold options"):
        scaffold_argv("vite_react", "frontend", {"shell": "rm -rf /"})


def test_template_recipes_write_files_under_the_root():
    files = dict(scaffold_files("fastapi", "backend"))
    assert "backend/app/main.py" in files
    assert "backend/requirements.txt" in files
    assert "include_router" in files["backend/app/main.py"]
    # single-root: no prefix
    files = dict(scaffold_files("express", "."))
    assert "package.json" in files and "src/index.js" in files


def test_template_recipe_rejects_argv_frameworks():
    with pytest.raises(ValueError, match="argv recipe"):
        scaffold_files("vite_react", "frontend")
