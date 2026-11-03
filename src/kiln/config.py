"""Runtime configuration. Secrets are read from the environment or .env."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    model_api_key: str = ""
    meta_base_url: str = "https://api.meta.ai/v1"
    spark_model: str = "muse-spark-1.3"
    voice_model: str = "muse-voice-transcribe-1.0"

    open_weight_base_url: str = ""
    open_weight_api_key: str = ""
    open_weight_model: str = "meta-models/muse-spark-open-1026"

    # Optional ceiling for ONE model call. Default None = omit max_tokens and let
    # the model use its native default (verified generous: Muse Spark produced
    # 10k+ completion tokens uncapped, finish=stop). Length is guided by each
    # role's system prompt (RoleDefinition.output_guidance), not pinned here. Set
    # a value in .env only if you want a hard runaway guard.
    max_output_tokens: int | None = Field(default=None, gt=0)
    run_token_ceiling: int = Field(default=200_000, gt=0)
    project_token_ceiling: int = Field(default=2_000_000, gt=0)

    sandbox_image: str = "kiln-sandbox:latest"
    sandbox_timeout_seconds: int = Field(default=120, gt=0)
    sandbox_memory: str = "512m"
    sandbox_cpus: str = "1.0"

    # How users get the companion. Until the package is on PyPI, the honest
    # default is uvx-from-git; from a checkout, .venv\Scripts\kiln-companion works.
    companion_command_prefix: str = (
        "uvx --from git+https://github.com/Uknowme-h/kiln kiln-companion"
    )

    # Tests and pairing rehearsals turn this off so a companion attach does
    # not fire a real pipeline run.
    pipeline_autostart: bool = True

    # Dev agents are tool-using loops that work on the user's machine; turn
    # this off to fall back to the one-shot structured-output workers.
    agentic_devs: bool = True
    # The requirements analyst may ask the user a few questions first.
    ask_requirements: bool = True
    # How many dev agents may work at once. Two keeps path ownership easy to
    # reason about; the planner still decides which tasks are independent.
    max_parallel_devs: int = Field(default=2, ge=1, le=8)
    # Tests run on the user's machine by default; "docker" is the opt-in sandbox.
    sandbox: str = "host"
    # Web search provider on the companion: "exa" (hosted, no key) or "duckduckgo".
    web_search_provider: str = "exa"

    # Persist per-run debug logs under .kiln/runs/{run_id}/ (transcript, usage, events).
    # Turn off in production / when disk noise is unwanted.
    debug: bool = False
    debug_log_dir: str = ".kiln/runs"

    # Coding-quality features (OpenCode-style gaps). Defaults favor quality in dev.
    inject_product_briefing: bool = True
    enable_explore_subagent: bool = True
    explore_max_steps: int = Field(default=8, ge=2, le=20)
    explore_max_per_task: int = Field(default=2, ge=0, le=8)
    enable_todo_tool: bool = True
    todo_probe_every: int = Field(default=5, ge=1, le=30)
    enable_project_skills: bool = True
    post_edit_diagnostics: bool = True
    enable_compaction: bool = False
    compact_threshold_chars: int = Field(default=100_000, ge=20_000)


def load_settings() -> Settings:
    """Load .env. A file that is only the raw key is accepted as MODEL_API_KEY."""
    env_path = Path(".env")
    if env_path.is_file():
        lines = [
            line.strip()
            for line in env_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if len(lines) == 1 and "=" not in lines[0]:
            return Settings(_env_file=None, model_api_key=lines[0])
    return Settings()
