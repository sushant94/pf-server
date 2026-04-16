from functools import cached_property
from pathlib import Path
from typing import Literal

import httpx
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def fetch_github_id(username: str) -> int:
    """Fetch GitHub user ID from username via GitHub API."""
    try:
        response = httpx.get(f"https://api.github.com/users/{username}", timeout=10.0)
        response.raise_for_status()
        user_data = response.json()
        return user_data["id"]
    except Exception as e:
        raise ValueError(f"Failed to fetch GitHub ID for username '{username}': {e}")


def parse_allowed_ids(v: str | set) -> set[int]:
    """Parse comma-separated GitHub usernames and fetch their IDs."""
    if isinstance(v, set):
        return v
    if not v:
        return set()
    usernames = [x.strip() for x in str(v).split(",") if x.strip()]
    ids = set()
    for username in usernames:
        user_id = fetch_github_id(username)
        ids.add(user_id)
    return ids


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    Required env vars: GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, JWT_SECRET
    Optional: DEPLOYMENT_TYPE (dev|prod), ALLOWED_GITHUB_IDS_DEV,
              ALLOWED_GITHUB_IDS_PROD, TAR_EXTRACTION_MODE, etc.
    """

    # pf:invariant:Settings.jwt_secret_minimum jwt_secret must be at least 32 characters for HS256 security
    # pf:invariant:Settings.positive_constraints max_tar_size_mb, lite_analysis_debounce_ms, jwt_expiry_hours must be > 0
    # pf:invariant:Settings.tar_mode_valid tar_extraction_mode must be 'docker' or 'mounted'
    model_config = SettingsConfigDict(env_file=".env")

    github_client_id: str = Field(..., min_length=1)
    github_client_secret: str = Field(..., min_length=1)
    jwt_secret: str = Field(..., min_length=32)  # HS256 requires sufficient entropy
    jwt_expiry_hours: int = Field(default=24 * 7, gt=0)  # 1 week, must be positive
    # Deployment type: "dev" or "prod" (defaults to "dev" if not set)
    deployment_type: str = Field(default="dev", validation_alias="DEPLOYMENT_TYPE")

    # Dev and prod specific GitHub username lists
    allowed_github_ids_dev_raw: str = Field(
        default="", validation_alias="ALLOWED_GITHUB_IDS_DEV"
    )
    allowed_github_ids_prod_raw: str = Field(
        default="", validation_alias="ALLOWED_GITHUB_IDS_PROD"
    )
    container_image: str = Field(default="pf-user-container:latest", min_length=1)
    container_network: str = Field(default="pf-internal", min_length=1)

    # Path settings (see containers.py for usage)
    docker_base_cwd: Path = Path("/workdir")
    docker_pf_tools_directory: Path = Path("/pf-tools")
    docker_opencode_directory: Path = Path("/opencode")
    host_users_data_directory: Path = Path.home() / "pf_users_data"
    host_pf_tools_directory: Path = Path.home() / "csf" / "pytest-property-checker"
    host_opencode_directory: Path = Path.home() / "csf" / "opencode"

    # Tar sync extraction mode: "docker" or "mounted"
    tar_extraction_mode: Literal["docker", "mounted"] = "mounted"

    # Max tar archive size in MB (must be positive)
    max_tar_size_mb: int = Field(default=100, gt=0)

    # Analysis settings (debounce must be positive)
    lite_analysis_debounce_ms: int = Field(default=500, gt=0)
    lite_analysis_command: str = "pf -q --log-level='debug' mine --no-sandbox -c /pf-tools/proofactory/configs/{config_name} guess . --resume-with-feedback {feedback_file}"

    # Analysis backends: "pf" for pf-tools or "opencode" for opencode agent
    # Each analysis type can use a different backend
    lite_analysis_backend: Literal["pf", "opencode"] = Field(default="pf")
    trigger_analysis_backend: Literal["pf", "opencode"] = Field(default="pf")
    ask_analysis_backend: Literal["pf", "opencode"] = Field(default="pf")

    # Docker log polling interval in seconds
    docker_log_poll_interval: float = Field(default=5.0, gt=0)

    # Logging settings
    log_level: str = Field(default="DEBUG")  # Use INFO in production
    log_json: bool = Field(default=False)  # True for production JSON output

    # Options for LLM models
    # pf:requires:validate_jwt_secret.minimum_length input must be at least 32 characters
    # pf:ensures:validate_jwt_secret.preserves_value returns input unchanged if valid
    model_name: str = Field(
        default="bedrock/us.anthropic.claude-opus-4-5-20251101-v1:0"
    )

    # MCP API keys (passed to containers)
    context7_api_key: str = Field(
        default="FAKE_CTX7_API_KEY",
        validation_alias="CONTEXT7_API_KEY",
    )

    @field_validator("jwt_secret")
    @classmethod
    def validate_jwt_secret(cls, v: str) -> str:
        """Ensure JWT secret has sufficient length for security."""
        if len(v) < 32:
            # pf:ensures:allowed_github_ids.deployment_dependent returns prod IDs if is_production, dev IDs otherwise
            raise ValueError(
                "JWT_SECRET must be at least 32 characters for HS256 security"
            )
        return v

    @cached_property
    def allowed_github_ids(self) -> set[int]:
        """Get allowed GitHub IDs as a set based on deployment type.

        Priority:
        1. If DEPLOYMENT_TYPE is "prod", use ALLOWED_GITHUB_IDS_PROD
        2. If DEPLOYMENT_TYPE is "dev", use ALLOWED_GITHUB_IDS_DEV
        3. Default to dev if deployment type not specified
        """
        deployment = self.deployment_type.lower()

        if deployment == "prod":
            # pf:ensures:port.prod_8001 returns 8001 if is_production
            # pf:ensures:port.dev_8000 returns 8000 if not is_production
            # Production: use prod-specific usernames
            return parse_allowed_ids(self.allowed_github_ids_prod_raw)
        else:
            # Dev: use dev-specific usernames
            return parse_allowed_ids(self.allowed_github_ids_dev_raw)

    @cached_property
    def is_production(self) -> bool:
        """Check if the deployment type is production."""
        return self.deployment_type.lower() == "prod"

    @cached_property
    def port(self) -> int:
        """Get server port based on deployment type."""
        return 8001 if self.is_production else 8000


settings = Settings()
