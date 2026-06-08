from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8-sig", extra="ignore")

    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-5.5", alias="OPENAI_MODEL")
    finmind_token: str = Field(default="", alias="FINMIND_TOKEN")
    news_api_key: str = Field(default="", alias="NEWS_API_KEY")
    eia_api_key: str = Field(default="", alias="EIA_API_KEY")
    serpapi_api_key: str = Field(default="", alias="SERPAPI_API_KEY")
    tavily_api_key: str = Field(default="", alias="TAVILY_API_KEY")
    brave_search_api_key: str = Field(default="", alias="BRAVE_SEARCH_API_KEY")
    default_ticker: str = Field(default="2603", alias="DEFAULT_TICKER")
    request_timeout: float = Field(default=20.0, alias="REQUEST_TIMEOUT_SECONDS")
    openai_timeout_seconds: float = Field(default=180.0, alias="OPENAI_TIMEOUT_SECONDS")
    openai_max_output_tokens: int = Field(default=4000, alias="OPENAI_MAX_OUTPUT_TOKENS")


@lru_cache
def get_settings() -> Settings:
    return Settings()
