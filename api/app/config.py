from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./cats.db"
    secret_key: str = "change-me"
    access_token_expire_minutes: int = 60 * 24 * 7  # 7 days
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
    anthropic_model: str = "claude-sonnet-4-6"
    # Transactional email (Resend). When resend_api_key is empty, email sending
    # is skipped and the reset code is logged instead (local dev). email_from
    # must be an address on a domain verified in Resend.
    resend_api_key: str = ""
    email_from: str = "Cats <no-reply@catapp.uk>"
    # Branding for the shared email layout (header logo + footer). The logo is
    # served by this API from /static/logo.png (see main.py), so it has a stable
    # public URL email clients can fetch.
    email_logo_url: str = "https://api.catapp.uk/static/logo.png"
    email_brand_name: str = "Cats"
    email_company: str = "Amicitia Ltd"
    email_support: str = "support@catapp.uk"
    email_website: str = "https://catapp.uk"


settings = Settings()
