from functools import lru_cache
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "HalalSwipe"
    app_secret: str = Field(default="dev-secret-change-me", alias="APP_SECRET")
    base_url: str = Field(default="http://127.0.0.1:8000", alias="BASE_URL")
    database_url: str = Field(default="app.db", alias="DATABASE_URL")
    admin_email: str = Field(default="admin@example.com", alias="ADMIN_EMAIL")
    demo_mode: bool = Field(default=True, alias="DEMO_MODE")
    stripe_secret_key: str = Field(default="", alias="STRIPE_SECRET_KEY")
    stripe_webhook_secret: str = Field(default="", alias="STRIPE_WEBHOOK_SECRET")
    stripe_price_id: str = Field(default="", alias="STRIPE_PRICE_ID")
    google_client_id: str = Field(default="", alias="GOOGLE_CLIENT_ID")
    free_contact_limit: int = Field(default=5, alias="FREE_CONTACT_LIMIT")
    static_version: str = Field(default="20260628-15", alias="STATIC_VERSION")
    upload_dir: Path = Path("uploads")

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    (settings.upload_dir / "profile_images").mkdir(parents=True, exist_ok=True)
    (settings.upload_dir / "imports").mkdir(parents=True, exist_ok=True)
    return settings
