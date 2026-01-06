from functools import cached_property

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def parse_allowed_ids(v: str | set) -> set[int]:
    """Parse comma-separated GitHub IDs into a set of integers."""
    if isinstance(v, set):
        return v
    if not v:
        return set()
    return {int(x.strip()) for x in str(v).split(",") if x.strip()}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    github_client_id: str
    github_client_secret: str
    jwt_secret: str
    jwt_expiry_hours: int = 24 * 7  # 1 week
    allowed_github_ids_raw: str = Field(
        default="", validation_alias="ALLOWED_GITHUB_IDS"
    )
    container_image: str = "pf-user-container:latest"
    container_network: str = "pf-internal"

    @cached_property
    def allowed_github_ids(self) -> set[int]:
        """Get allowed GitHub IDs as a set."""
        return parse_allowed_ids(self.allowed_github_ids_raw)


settings = Settings()
