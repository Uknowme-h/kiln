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

    max_output_tokens: int = Field(default=4096, gt=0)
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
