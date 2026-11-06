from typing import Literal

from cryptography.fernet import Fernet
from pydantic import field_validator
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
    # Second Observium that monitors the cell-site routers; same credentials as the main one.
    observium_csr_url: str = "http://172.17.12.112"
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

    # RANCID repositories published through gitweb (all repos: CSR, METRO, OLT, BNG, ...). Backup source
    # for configs, and the only source for devices Observium does not monitor (cell-site routers).
    gitweb_url: str | None = None  # e.g. http://172.17.12.232/gitweb/
    gitweb_user: str | None = None
    gitweb_password: str | None = None
    gitweb_verify_tls: bool = True
    gitweb_workers: int = 8  # parallel blob downloads
    gitweb_exclude_repos: list[str] = []
    gitweb_create_devices: bool = True

    # In-process daily scheduler. With several replicas each day's run is claimed once in the DB;
    # set false where an external scheduler (Kubernetes CronJob, cron) runs the CLI instead.
    scheduler_enabled: bool = True
    schedule_timezone: str = "Asia/Jakarta"
    gitweb_daily_at: str | None = "02:30"  # HH:MM; empty disables the daily gitweb update

    # Link hotspots (utilization of the busiest links/ports): warning and critical levels.
    hotspot_warning_pct: float = 70.0
    hotspot_critical_pct: float = 90.0
    hotspot_suspect_pct: float = 150.0  # above this the link capacity is treated as wrong data
    # ONTs one GPON port takes at its split ratio (1:64): a PON port's occupancy is its online ONTs against this.
    pon_max_onts: int = 64

    # Collector settings page (Pengaturan Collector). Values saved there override this environment.
    # Only admins can change it; set false in production to keep credentials env-only.
    settings_ui_enabled: bool = True
    # Key for the secrets saved from that page (Fernet, urlsafe base64). Without it a key file is created
    # next to the backups; with several replicas set the same key everywhere (see docs/LAUNCH.md).
    secret_key: str | None = None

    # AI assistant (Phase 9). A local Ollama provider is created on first start when none exists;
    # hosted providers (Claude, OpenAI, Azure OpenAI, ...) are added from the AI Assistant page.
    ai_seed_local: bool = True
    ai_ollama_url: str = "http://localhost:11434"
    ai_default_model: str = "llama3.1:8b"
    ai_context_chars: int = 12000  # network data sent with each question (local models have small contexts)

    # Login and roles. With auth off (tests, single-user dev) every request acts as a local admin.
    auth_enabled: bool = True
    session_hours: float = 12.0
    # Built-in local admin, created on first start when there are no users. Without a password here, set one with
    # `python -m app.reset_password`. Either way it must be changed at first login.
    admin_username: str = "admin"
    admin_password: str | None = None
    password_min_length: int = 10
    # Failed logins within login_lock_minutes. Per username + address, so an attacker elsewhere cannot lock the owner
    # out; per account, higher, against guessing from many addresses; per address, against trying many usernames.
    login_max_attempts: int = 5
    login_account_max_attempts: int = 50
    login_ip_max_failures: int = 30
    login_lock_minutes: int = 15
    # Proxies in front of this process that append to X-Forwarded-For; the client address is that many entries from
    # the right, since anything further left is what the client itself sent. 0 = not behind a proxy: the header is
    # ignored. Too low makes every user share the proxy's address, and with it the login limit per address.
    trusted_proxy_hops: int = 0
    ingest_token: str | None = None  # Bearer token for external systems posting to /api/integrations
    # Bearer token for schedulers starting syncs (Kubernetes CronJobs) on /api/collectors. Acts as an engineer, so
    # collector credentials (admin) stay out of reach.
    job_token: str | None = None
    # Swagger UI and the OpenAPI schema (/docs, /openapi.json). They need no login: turn off in production.
    docs_enabled: bool = True

    # Pushing changes to devices (SSH/telnet). Engineers type their own device credentials per run; they are
    # never stored. A jump host, when set, is reached with the same credentials unless a jump user is given.
    device_jump_host: str | None = None
    device_jump_port: int = 22
    device_jump_user: str | None = None
    device_timeout_s: float = 30.0
    # Host keys seen on first connect are kept here and enforced afterwards (default: next to the backups).
    device_known_hosts: str | None = None
    # false: a device whose key is not in that file is refused, so the first connect cannot be intercepted.
    device_ssh_trust_new_hosts: bool = True
    # Telnet sends device passwords in clear text. false once all devices take SSH.
    device_telnet_enabled: bool = True

    # Read-model cache for heavy views (topology, capacity, dashboard); cleared on every write.
    cache_ttl_s: float = 60.0
    db_pool_size: int = 10
    db_max_overflow: int = 20

    @field_validator("secret_key")
    @classmethod
    def _secret_key_is_a_fernet_key(cls, value: str | None) -> str | None:
        """Checked at start: otherwise a wrong key only fails once a secret is saved, and from then on no process
        starts."""
        if value:
            try:
                Fernet(value.encode())
            except ValueError as exc:
                raise ValueError("NADIEN_SECRET_KEY harus kunci Fernet: python -c \"from cryptography.fernet import "
                                 "Fernet; print(Fernet.generate_key().decode())\"") from exc
        return value


settings = Settings()
