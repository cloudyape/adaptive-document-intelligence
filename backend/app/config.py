"""Runtime configuration, loaded from environment variables / .env (prefix ADI_)."""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="ADI_", extra="ignore")

    # --- Storage ---
    database_url: str = "postgresql+psycopg://adi:adi@localhost:5432/adi"
    storage_dir: Path = Path("storage")

    # --- Microsoft Graph (Outlook) ---
    graph_tenant_id: str | None = None
    graph_client_id: str | None = None
    graph_client_secret: str | None = None
    graph_mailbox: str | None = None  # mailbox UPN to read, e.g. accounts@company.com
    graph_folder: str = "inbox"
    graph_poll_enabled: bool = False
    graph_poll_seconds: int = 300
    graph_max_messages_per_run: int = 50

    # --- Claude (LLM tiers). Credentials: ANTHROPIC_API_KEY or an `ant auth login` profile. ---
    llm_enabled: bool = True
    model_classifier: str = "claude-haiku-5-5"  # doc-type triage when keywords are ambiguous
    model_ocr_llm: str = "claude-haiku-5-5"  # MEDIUM tier: OCR text / page images -> JSON
    model_document_ai: str = "claude-sonnet-5-5"  # DIFFICULT tier when Azure DI isn't configured
    model_fallback: str = "claude-opus-5-5"  # last automatic resort before a human

    # --- Azure AI Document Intelligence (optional DIFFICULT-tier engine) ---
    azure_di_endpoint: str | None = None
    azure_di_key: str | None = None
    azure_di_cost_per_page: float = 0.01

    # --- OCR / rendering ---
    tesseract_cmd: str | None = None  # autodetected on PATH when unset
    render_dpi: int = 200
    max_pages_for_vision: int = 20

    # --- Orchestration thresholds ---
    accept_confidence: float = 0.85  # accept an engine's result at/above this validation score
    amount_tolerance: float = 1.0  # absolute currency tolerance for arithmetic checks
    max_cost_per_document_usd: float = 0.50  # stop escalating once a document has cost this much


@lru_cache
def get_settings() -> Settings:
    return Settings()
