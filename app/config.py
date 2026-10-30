from pydantic import AliasChoices, Field

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    secret_key: str = Field(
        default="dev-insecure-change-me",
        validation_alias=AliasChoices("SECRET_KEY", "AUTH_SECRET"),
    )
    database_url: str = "sqlite:///./app.db"
    cors_origins: str = "http://localhost:3000"
    seed_admin_email: str | None = None
    seed_admin_password: str | None = None

    # Outbound mail (SMTP)
    smtp_host: str | None = Field(default=None, validation_alias=AliasChoices("SMTP_HOST"))
    smtp_port: int = Field(default=587, validation_alias=AliasChoices("SMTP_PORT"))
    smtp_user: str | None = Field(default=None, validation_alias=AliasChoices("SMTP_USER", "SMTP_USERNAME"))
    smtp_password: str | None = Field(default=None, validation_alias=AliasChoices("SMTP_PASSWORD"))
    smtp_use_tls: bool = Field(
        default=True,
        validation_alias=AliasChoices("SMTP_USE_TLS", "SMTP_STARTTLS"),
        description="Use STARTTLS after connect (typical for port 587). Ignored when SMTP_USE_SSL=true.",
    )
    smtp_use_ssl: bool = Field(
        default=False,
        validation_alias=AliasChoices("SMTP_USE_SSL"),
        description="Use implicit TLS (SMTP_SSL, typical for port 465). When true, STARTTLS is not used.",
    )
    smtp_from: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SMTP_FROM", "SMTP_FROM_EMAIL", "EMAIL_FROM"),
        description="From address shown to recipients (e.g. noreply@yourdomain.com).",
    )
    email_approval_notify_to: str | None = Field(
        default=None,
        description="Comma-separated admin inboxes to notify when a new student registers (optional).",
        validation_alias=AliasChoices("EMAIL_APPROVAL_NOTIFY_TO", "EMAIL_ADMIN_NOTIFY"),
    )
    # Web client ID from Google Cloud Console (OAuth 2.0) — used to verify Sign in with Google ID tokens.
    google_oauth_client_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_CLIENT_ID"),
    )
    # Telegram Gateway API token (https://gateway.telegram.org) — sends phone verification codes.
    telegram_gateway_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("TELEGRAM_GATEWAY_TOKEN", "TELEGRAM_GATEAWAY_TOKEN"),
    )
    # Meta WhatsApp Cloud API webhook: verify token you type in the Meta app dashboard, and the app secret
    # (App settings → Basic) used to check the X-Hub-Signature-256 header on incoming events.
    whatsapp_verify_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("WHATSAPP_VERIFY_TOKEN"),
    )
    whatsapp_app_secret: str | None = Field(
        default=None,
        validation_alias=AliasChoices("WHATSAPP_APP_SECRET", "META_APP_SECRET"),
    )
    public_app_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PUBLIC_APP_BASE_URL", "APP_PUBLIC_URL"),
    )

    openai_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENAI_API_KEY"),
    )
    openai_model: str = Field(
        default="gpt-4o",
        validation_alias=AliasChoices("OPENAI_MODEL"),
    )
    openai_vision_model: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENAI_VISION_MODEL"),
        description="Model for PDF page images (defaults to OPENAI_MODEL). Use a vision-capable model.",
    )
    pdf_import_page_dpi: int = Field(
        default=200,
        validation_alias=AliasChoices("PDF_IMPORT_PAGE_DPI"),
    )

    @property
    def openai_vision_model_effective(self) -> str:
        """Vision model for PDF page images; falls back to OPENAI_MODEL then gpt-4o."""
        explicit = (self.openai_vision_model or "").strip()
        if explicit:
            return explicit
        base = (self.openai_model or "").strip()
        return base if base else "gpt-4o"

    @property
    def email_approval_notify_recipients(self) -> list[str]:
        if not self.email_approval_notify_to:
            return []
        return [x.strip() for x in self.email_approval_notify_to.split(",") if x.strip()]


settings = Settings()
