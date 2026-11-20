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

    # --- Azure AI Document Intelligence (the only paid engine; tiers 3-5) ---
    azure_di_endpoint: str | None = None  # https://<resource>.cognitiveservices.azure.com
    azure_di_key: str | None = None
    azure_di_api_version: str = "2024-11-30"
    azure_di_read_enabled: bool = True  # tier 3: prebuilt-read OCR + local rules (cheapest Azure call)
    azure_di_highres_fallback: bool = True  # tier 5: prebuilt-invoice + ocrHighResolution add-on
    # Per-page list prices in USD. Defaults are placeholders: set them from your Azure pricing / portal.
    azure_di_read_cost_per_page: float = 0.0015
    azure_di_invoice_cost_per_page: float = 0.01
    azure_di_highres_cost_per_page: float = 0.006  # add-on, charged on top of the invoice model
    azure_di_timeout_seconds: int = 120
    # Rate limits: all workers share one pace. F0 (free) allows ~20 requests/min (polls count); S0 allows far more.
    azure_di_requests_per_minute: int = 15
    azure_di_poll_seconds: float = 3.0  # wait between "is it done yet?" checks
    azure_di_max_retries: int = 5  # on 429 / 5xx / dropped connections, with backoff
    azure_di_max_file_mb: float = 4.0  # F0 rejects files over 4 MB; S0 allows 500 MB

    # --- OCR / rendering ---
    tesseract_cmd: str | None = None  # autodetected on PATH when unset
    render_dpi: int = 200

    # --- Orchestration thresholds ---
    accept_confidence: float = 0.85  # accept an engine's result at/above this validation score
    amount_tolerance: float = 1.0  # absolute currency tolerance for arithmetic checks
    max_cost_per_document_usd: float = 0.25  # stop escalating once a document has cost this much


@lru_cache
def get_settings() -> Settings:
    return Settings()
