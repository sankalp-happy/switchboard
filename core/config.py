from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    GROQ_API_KEY: str = ""
    GOOGLE_API_KEY: str = ""
    REDIS_URL: str = "redis://localhost:6379/0"
    PORT: int = 8000
    HOST: str = "0.0.0.0"

    class Config:
        env_file = ".env"
        extra = "allow"

settings = Settings()
