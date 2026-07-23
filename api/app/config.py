from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./cats.db"
    secret_key: str = "change-me"
    # Tokens are rotated by the app on every launch (POST /auth/refresh), so
    # this is the maximum time a device can stay *closed* before its session
    # lapses — not a cap on session length.
    access_token_expire_minutes: int = 60 * 24 * 30  # 30 days
    # Cross-origin policy. The API authenticates with Bearer tokens (not cookies),
    # so credentials are never needed and a wildcard origin is safe. Set a
    # comma-separated allowlist to lock the browser (web) build down further.
    cors_allow_origins: str = "*"
    # OAuth client IDs. When set, Apple/Google sign-in verifies the token's
    # audience against these — rejecting tokens minted for a different app
    # (prevents token-substitution account takeover). Left blank in local dev,
    # where audience verification is skipped with a warning. google_client_id
    # accepts a comma-separated list (a native app has one client id per
    # platform: iOS, Android, and optionally Web).
    google_client_id: str = ""
    apple_client_id: str = ""
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    # Public base for media URLs (e.g. a CloudFront domain). When set, S3 keys
    # are served as "{media_base_url}/{key}"; when empty, S3 falls back to
    # presigned URLs and local dev serves via the API's /uploads route.
    media_base_url: str = ""
    # Background reconciliation of the DB against the storage bucket. Sweeps
    # orphaned objects (uploaded but never committed) older than the grace
    # window, and logs dangling DB references whose object is missing.
    storage_reconcile_enabled: bool = True
    storage_reconcile_interval_hours: int = 6
    storage_orphan_grace_hours: int = 24
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5"
    # Transactional email (Resend). When resend_api_key is empty, email sending
    # is skipped and the reset code is logged instead (local dev). email_from
    # must be an address on a domain verified in Resend.
    resend_api_key: str = ""
    email_from: str = "Meow Map <no-reply@catapp.uk>"
    # Branding for the shared email layout (header logo + footer). The logo is
    # served by this API from /static/logo.png (see main.py), so it has a stable
    # public URL email clients can fetch.
    email_logo_url: str = "https://api.catapp.uk/static/logo.png"
    email_brand_name: str = "Meow Map"
    email_company: str = "Amicitia Ltd"
    email_support: str = "support@catapp.uk"
    email_website: str = "https://catapp.uk"


settings = Settings()
