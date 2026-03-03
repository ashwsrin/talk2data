from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    oci_config_file: str
    oci_profile: str
    compartment_id: str
    region: str = "us-chicago-1"  # Default OCI region
    backend_url: str = "http://localhost:8001"  # Base URL for file_url so OCI can fetch PDFs (env: BACKEND_URL)
    database_url: str = "sqlite+aiosqlite:///./local_app.db"  # DB connection string (env: DATABASE_URL); restart required to change
    
    # Oracle Autonomous Database for App Persistence
    oracle_db_dsn: str = ""
    oracle_db_user: str = ""
    oracle_db_password: str = ""
    oracle_wallet_path: str = ""
    oracle_wallet_password: str | None = None

    # NL2SQL MCP Server Settings
    oracle_nl2sql_password: str = ""  # Oracle Autonomous DB password for NL2SQL (env: ORACLE_NL2SQL_PASSWORD); never returned by API

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )


settings = Settings()
