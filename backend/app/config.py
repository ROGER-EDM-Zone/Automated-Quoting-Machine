"""Runtime settings.

Rates and business rules are deliberately absent from this file — they live in
`rate_table` / `rules_table` and are edited through the admin UI (spec section
6: "A rate change is a data edit, not a deployment").
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="AQM_", extra="ignore"
    )

    environment: str = "development"

    # Azure SQL / Postgres in production; SQLite locally and in tests.
    database_url: str = "sqlite:///./aqm.db"
    sql_echo: bool = False

    # Blob storage. `local` writes under storage_root; `azure` uses the
    # container + connection string.
    storage_backend: str = "local"
    storage_root: str = "./storage"
    azure_storage_connection_string: str | None = None
    azure_storage_container: str = "quoting-attachments"

    # --- Anthropic (extraction, classification, note intent) ---
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-opus-5"
    anthropic_max_tokens: int = 8000
    # Rasterise drawings in this band before the vision call (spec stage 2).
    drawing_dpi: int = 175

    # --- Confidence policy (spec section 8: open decision, so configurable) ---
    #: Default floor. A field scoring below this is withheld from pricing and
    #: renders as "unread" rather than as a value.
    confidence_threshold_default: float = 0.80
    #: Per-field overrides, e.g. {"tightest_tolerance": 0.9}.
    confidence_threshold_overrides: dict[str, float] = Field(
        default_factory=lambda: {
            "material": 0.85,
            "tightest_tolerance": 0.90,
            "quantity": 0.90,
            "heat_treatment": 0.85,
        }
    )

    # --- Microsoft Graph (intake + draft reply) ---
    graph_tenant_id: str | None = None
    graph_client_id: str | None = None
    graph_client_secret: str | None = None
    graph_quoting_mailbox: str | None = None
    #: Only mail carrying this Outlook category enters the pipeline.
    graph_rfq_category: str = "RFQ"
    graph_webhook_client_state: str | None = None

    # --- Auth (Entra ID SSO) ---
    auth_required: bool = False
    entra_tenant_id: str | None = None
    entra_audience: str | None = None

    def threshold_for(self, field_name: str) -> float:
        """Confidence floor for one extracted field."""
        return self.confidence_threshold_overrides.get(
            field_name, self.confidence_threshold_default
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
