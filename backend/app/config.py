from typing import Literal

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

    # Database backup taken before every collector run that changes inventory/config data.
    # Default directory: `nadien-backups/` next to a SQLite file, `./backups` for PostgreSQL.
    backup_dir: str | None = None
    backup_keep: int = 15
    backup_before_sync: bool = True

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
    # LLDP is the primary topology source; CDP fills in Cisco boxes that only speak CDP.
    # When both report the same local port, LLDP wins. MNDP/ISDP are never used.
    observium_include_cdp: bool = True

    # Observium RRD history (daily peaks per port) for utilization trends.
    # "api": graph.php JSON with the REST API login (no extra access). "ssh" / "local": rrdtool xport
    # of the exact daily MAX archive on the Observium server, over SSH or when NADIEN runs there.
    observium_rrd_source: Literal["api", "ssh", "local"] = "api"
    observium_graph_workers: int = 6  # parallel graph.php requests; each one runs rrdtool on Observium
    observium_rrd_path: str = "/opt/observium/rrd"
    observium_ssh_host: str | None = None  # default: the host of NADIEN_OBSERVIUM_URL
    observium_ssh_port: int = 22
    observium_ssh_user: str | None = None
    observium_ssh_key_file: str | None = None
    observium_ssh_password: str | None = None
    observium_ssh_known_hosts: str | None = None
    # Growth used for exhaustion forecasts: least-squares trend over this many days of samples.
    trend_window_days: int = 90

    # RANCID config archive used by Observium (Observium's API does not expose configs).
    # Either a local/mounted path, or SFTP to the Observium server with a read-only SSH account.
    rancid_path: str | None = None
    rancid_ssh_host: str | None = None
    rancid_ssh_port: int = 22
    rancid_ssh_user: str | None = None
    rancid_ssh_key_file: str | None = None
    rancid_ssh_password: str | None = None
    rancid_remote_path: str = "/opt/observium/rancid"
    # Unknown host keys are rejected unless listed here (or in ~/.ssh/known_hosts).
    rancid_ssh_known_hosts: str | None = None

    # Oxidized (alternative config source).
    oxidized_url: str | None = None
    oxidized_user: str | None = None
    oxidized_password: str | None = None


settings = Settings()
