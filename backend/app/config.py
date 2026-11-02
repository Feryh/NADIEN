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

    # Observium (devices, ports, LLDP neighbours, addresses, port rates).
    # Credentials come only from the environment / .env — never commit them.
    # Prefer a read-only API token restricted to this server's IP over a user password.
    observium_url: str = "http://172.17.42.136"
    observium_token: str | None = None
    observium_user: str | None = None
    observium_password: str | None = None
    observium_verify_tls: bool = True
    observium_timeout_s: float = 60.0
    # POP code is taken from the hostname with this regex (first group), e.g. "JKT-P-01" -> "JKT".
    observium_pop_regex: str = r"^([A-Za-z]{3,4})[-_.]"
    # LLDP is the mandatory topology source; CDP adjacencies are only imported when enabled.
    observium_include_cdp: bool = False

    # Oxidized (device configurations — Observium's API does not expose configs).
    oxidized_url: str | None = None
    oxidized_user: str | None = None
    oxidized_password: str | None = None


settings = Settings()
