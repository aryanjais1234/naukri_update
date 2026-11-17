from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]

class Settings(BaseSettings):
    openai_api_key: str = ""
    openai_model: str = "gpt-6-luna"
    database_url: str = f"sqlite:///{ROOT / 'data' / 'naukri_updater.db'}"
    browser_profile_dir: str = str(ROOT / "data" / "naukri-chrome-profile")
    frontend_origin: str = "http://localhost:5174"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
Path(ROOT / "data").mkdir(exist_ok=True)
