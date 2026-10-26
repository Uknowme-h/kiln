from kiln.agents.roles import Role
from kiln.config import Settings, load_settings
from kiln.gateway.routing import route_for


def _settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "model_api_key": "spark-key",
        "meta_base_url": "https://api.meta.ai/v1",
        "spark_model": "muse-spark-1.3",
        "open_weight_base_url": "",
        "open_weight_api_key": "",
        "open_weight_model": "meta-models/muse-spark-open-1026",
    }
    values.update(overrides)
    return Settings(**values)


def test_planning_roles_use_muse_spark():
    settings = _settings()
    route = route_for(Role.REQUIREMENTS, settings)
    assert route.model == "muse-spark-1.3"
    assert route.provider == "meta"
    assert route.fallback is False
    assert "spark-key" not in route.public().values()


def test_coding_roles_fall_back_to_muse_spark_until_open_weight_is_configured():
    route = route_for(Role.WORKER, _settings())
    assert route.model == "muse-spark-1.3"
    assert route.fallback is True


def test_coding_roles_use_open_weight_when_the_endpoint_is_set():
    settings = _settings(
        open_weight_base_url="https://example.baseten.co/v1/",
        open_weight_api_key="open-key",
    )
    route = route_for(Role.FIXER, settings)
    assert route.provider == "open_weight"
    assert route.model == "meta-models/muse-spark-open-1026"
    assert route.base_url == "https://example.baseten.co/v1"
    assert route.public()["configured"] is True
    assert "open-key" not in str(route.public())


def test_a_one_line_env_file_is_the_model_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("local-test-key\n", encoding="utf-8")
    assert load_settings().model_api_key == "local-test-key"


def test_named_env_file_still_loads(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "MODEL_API_KEY=named-key\nSPARK_MODEL=muse-spark-1.1\n",
        encoding="utf-8",
    )
    settings = load_settings()
    assert settings.model_api_key == "named-key"
    assert settings.spark_model == "muse-spark-1.1"
