from decimal import Decimal
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # PostgreSQL
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "glassledger_user"
    postgres_password: str = "glassledger_password"
    postgres_db: str = "glassledger"
    postgres_uri: str = "postgresql://glassledger_user:glassledger_password@localhost:5432/glassledger"
    postgres_pool_min: int = 2
    postgres_pool_max: int = 10

    # MongoDB
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db_name: str = "glassledger"

    # Directory for the non-authoritative SQLite fallback stores
    sqlite_data_dir: str = "data"

    # Execution & Simulation
    default_broker: str = "paper"
    decimal_places: int = 4
    default_fee_rate: Decimal = Decimal("0.0005")  # 0.05%
    default_slippage: Decimal = Decimal("0.0100")  # 1 cent

    # Logging
    log_level: str = "INFO"


settings = Settings()
