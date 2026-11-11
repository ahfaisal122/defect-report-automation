"""All settings in one place. Values come from the environment or a local .env file."""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM (NFR-10: switching provider or model is a config change)
    llm_provider: str = "gemini"
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-3.5-flash-lite"  # pinned; rerun the eval when this changes
    llm_timeout_seconds: float = 60.0
    llm_cache_enabled: bool = True
    llm_cache_dir: Path = Path("var/llm_cache")

    # Prompt
    prompts_dir: Path = Path("prompts")
    prompt_version: str = "extraction_v1"

    # Routing (FR-06). The threshold is a starting value, to be tuned on the eval set.
    confidence_threshold: float = 0.85
    required_fields: list[str] = [
        "order_number",
        "part_number",
        "supplier",
        "defect_type",
        "defect_description",
        "quantity_affected",
        "severity",
        "detected_at",
        "detected_on",
        "reporter",
    ]

    # Review screen. No default: the app refuses to open until a password is set in .env.
    review_password: SecretStr | None = None

    # QM API and posting (FR-08, NFR-03)
    qm_api_url: str = "http://127.0.0.1:8001"
    qm_api_token: SecretStr = SecretStr("local-mock-service-account")  # mock only
    posting_max_retries: int = 3
    posting_backoff_seconds: float = 0.5
    posting_timeout_seconds: float = 5.0

    # Mock QM system: the trouble it makes on purpose
    mock_qm_db_path: Path = Path("var/mock_qm.db")
    mock_qm_port: int = 8001
    mock_qm_error_rate: float = 0.05
    mock_qm_spike_rate: float = 0.05
    mock_qm_spike_seconds: float = 1.0
    mock_qm_rate_limit_per_minute: int = 60

    # Cost (NFR-05). Prices are per million tokens. The free tier bills nothing; these
    # values price each call as if it were paid, so the cap also holds on a paid key.
    price_input_eur_per_mtok: float = 0.10
    price_output_eur_per_mtok: float = 0.40
    daily_cost_cap_eur: float = 1.00

    # Paths
    db_path: Path = Path("var/defects.db")
    input_dir: Path = Path("data/reports")
    master_dir: Path = Path("data/master")
    labels_path: Path = Path("data/eval/labels.json")
    log_path: Path = Path("var/logs/pipeline.jsonl")
    eval_results_dir: Path = Path("data/eval/results")  # kept in Git, to compare runs
    eval_work_dir: Path = Path("var/eval_work")


@lru_cache
def get_settings() -> Settings:
    return Settings()
