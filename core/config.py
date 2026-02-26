from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Provider keys (optional — prefer adding via Admin UI / DB)
    GROQ_API_KEY: str = ""
    GOOGLE_API_KEY: str = ""

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # Gateway
    PORT: int = 8000
    HOST: str = "0.0.0.0"

    # SQLite config store
    SQLITE_DB_PATH: str = "data/switchboard.db"

    # Fernet encryption key for API keys at rest
    # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    ENCRYPTION_KEY: str = ""

    class Config:
        env_file = ".env"
        extra = "allow"


settings = Settings()
