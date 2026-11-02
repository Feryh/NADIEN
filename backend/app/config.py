from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="NADIEN_", extra="ignore")

    database_url: str = "postgresql+psycopg://nadien:nadien@localhost:5432/nadien"
    cors_origins: list[str] = ["http://localhost:5173"]

    # Capacity color thresholds, applied to committed utilization (current + reserved).
    util_yellow_pct: float = 60.0
    util_red_pct: float = 80.0
    # Utilization at which a link is considered "needs upgrade" for planning.
    planning_threshold_pct: float = 80.0


settings = Settings()
