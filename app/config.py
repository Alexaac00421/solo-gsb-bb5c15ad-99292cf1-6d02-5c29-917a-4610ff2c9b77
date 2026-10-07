"""运行期配置。

全部配置项均可通过环境变量覆盖，默认值见 .env.example。
"""
from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(
        default="postgresql://fare:farepass@localhost:5432/fareclearing",
        alias="DATABASE_URL",
    )
    app_host: str = Field(default="0.0.0.0", alias="APP_HOST")
    app_port: int = Field(default=8080, alias="APP_PORT")

    # 运营日界（北京时间 04:00）
    day_boundary_hour: int = Field(default=4, alias="DAY_BOUNDARY_HOUR")
    # 相邻两程换乘判定间隔（分钟，含端点）
    transfer_window_minutes: int = Field(default=90, alias="TRANSFER_WINDOW_MINUTES")
    # 账户单运营日实收封顶（分）
    daily_cap_cents: int = Field(default=2000, alias="DAILY_CAP_CENTS")

    @field_validator("day_boundary_hour")
    @classmethod
    def _check_boundary(cls, v: int) -> int:
        if not 0 <= v <= 23:
            raise ValueError("DAY_BOUNDARY_HOUR must be between 0 and 23")
        return v

    @field_validator("transfer_window_minutes", "daily_cap_cents", "app_port")
    @classmethod
    def _check_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("value must be positive")
        return v


settings = Settings()
