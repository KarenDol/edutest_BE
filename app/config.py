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
    public_app_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PUBLIC_APP_BASE_URL", "APP_PUBLIC_URL"),
    )

    @property
    def email_approval_notify_recipients(self) -> list[str]:
        if not self.email_approval_notify_to:
            return []
        return [x.strip() for x in self.email_approval_notify_to.split(",") if x.strip()]


settings = Settings()
