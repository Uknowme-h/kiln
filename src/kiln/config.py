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
    # Developers working in parallel. The team is two developers (Dev A and
    # Dev B) who split the plan; 1 runs a single developer. Values above 2 are
    # accepted for older configs and mean two.
    max_parallel_devs: int = Field(default=2, ge=1, le=8)
    # A task whose developer ends with nothing delivered gets this many fresh
    # attempts in the same lane (told why the last one failed).
    worker_retries: int = Field(default=1, ge=0, le=3)
    # After those, the other developer gets one attempt before the task is
    # marked failed and the build carries on without it.
    worker_failover: bool = True
    # Longest a developer stays paused waiting for a shared file its teammate
    # holds (request_files wait=true). A paused loop spends no tokens.
    file_wait_seconds: int = Field(default=600, ge=10, le=3600)
    # Longest a developer stays paused waiting for its teammate's answer
    # (ask_peer wait=true). Also ends when the teammate's task ends.
    peer_answer_wait_seconds: int = Field(default=300, ge=10, le=3600)
    # The tech lead: an event-driven supervisor that answers escalations, asks
    # the user product questions, amends the plan's structure and arranges
    # installs. Each decision is one model call; the cap bounds the cost.
    # While no companion is attached, agents' tool calls are held (no model
    # calls) for up to this long before they fail; a restart reattaches.
    companion_outage_wait_seconds: int = Field(default=900, ge=0, le=7200)
    enable_supervisor: bool = True
    supervisor_max_calls: int = Field(default=12, ge=0, le=100)
    # How long an escalating developer stays paused for the decision (no tokens).
    supervisor_wait_seconds: int = Field(default=600, ge=10, le=3600)
    # How long the lead waits for the user before taking its recommended option.
    supervisor_user_timeout_seconds: int = Field(default=300, ge=30, le=3540)
    # Run one fixer per workspace root when the failing tests split cleanly by
    # root. Failures that span roots (contract mismatches) keep a single fixer.
    parallel_fixers: bool = True
    # Tests run on the user's machine by default; "docker" is the opt-in sandbox.
    sandbox: str = "host"
    # Auto-approve shell/install-style approval prompts so a run can proceed
    # unattended ("auto-approve dive"). CRITICAL SCOPE: this only covers the
    # command-approval path (PermissionEngine.ask). It NEVER answers ask_user
    # questions and NEVER approves the plan-approval beat — those stay manual.
    # Set via the companion's --auto-approve flag or AUTO_APPROVE=true in .env.
    auto_approve: bool = False
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
    enable_compaction: bool = True
    compact_threshold_chars: int = Field(default=100_000, ge=20_000)
    # OpenCode-style context hygiene: offload big companion outputs to disk,
    # and prune older tool bodies outside a recent protect window.
    tool_offload_chars: int = Field(default=8_000, ge=1_000)
    prune_protect_chars: int = Field(default=40_000, ge=5_000)


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
