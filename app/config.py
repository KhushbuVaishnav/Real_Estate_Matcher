"""
app/config.py

Centralized configuration, driven by environment variables (loaded from
.env via python-dotenv). This replaces the old pattern of flipping
USE_SAMPLE_DATA / USE_REALISTIC_DATA / USE_GENERATED_DATA booleans directly
in fetch_listings.py — in a real project you don't want to edit source code
to change which data source or AI provider is active, especially once this
runs anywhere besides your laptop (staging, CI, a teammate's machine).

Set these in your .env file:
    DATA_SOURCE=generated        # one of: live, sample, realistic, generated
    AI_PROVIDER=anthropic        # one of: anthropic, openai
    SCORE_THRESHOLD=60           # 0-100
    BATCH_SIZE=8
    CORS_ALLOW_ORIGINS=*         # comma-separated in production, e.g. https://yourapp.com
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent  # .../app
DATA_DIR = BASE_DIR / "data"


class Settings:
    # --- Data source ---
    # "live"      -> real SimplyRETS sandbox API
    # "sample"    -> app/data/sample_listings.json (small, hand-edited test set)
    # "realistic" -> app/data/realistic_listings.json (14 hand-written listings)
    # "generated" -> app/data/generated_listings.json (large generated set)
    DATA_SOURCE: str = os.environ.get("DATA_SOURCE", "generated").lower()

    SAMPLE_DATA_PATH: Path = DATA_DIR / "sample_listings.json"
    REALISTIC_DATA_PATH: Path = DATA_DIR / "realistic_listings.json"
    GENERATED_DATA_PATH: Path = DATA_DIR / "generated_listings.json"
    SCHOOLS_PATH: Path = DATA_DIR / "schools.json"

    SIMPLYRETS_BASE_URL: str = "https://api.simplyrets.com/properties"
    SIMPLYRETS_AUTH: tuple = ("simplyrets", "simplyrets")  # public sandbox creds

    # --- AI provider ---
    AI_PROVIDER: str = os.environ.get("AI_PROVIDER", "anthropic").lower()
    ANTHROPIC_API_KEY: str | None = os.environ.get("ANTHROPIC_API_KEY")
    OPENAI_API_KEY: str | None = os.environ.get("OPENAI_API_KEY")
    ANTHROPIC_MODEL: str = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
    OPENAI_MODEL: str = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

    # --- Matching behavior ---
    BATCH_SIZE: int = int(os.environ.get("BATCH_SIZE", 8))
    SCORE_THRESHOLD: int = int(os.environ.get("SCORE_THRESHOLD", 60))
    TEMPERATURE: float = float(os.environ.get("TEMPERATURE", 0.2))
    MAX_TOKENS: int = int(os.environ.get("MAX_TOKENS", 2000))

    # --- API / server ---
    CORS_ALLOW_ORIGINS: list = os.environ.get("CORS_ALLOW_ORIGINS", "*").split(",")
    DEFAULT_FETCH_LIMIT: int = int(os.environ.get("DEFAULT_FETCH_LIMIT", 1000))

    def validate(self):
        """Call at startup to fail fast on misconfiguration instead of erroring mid-request."""
        valid_sources = {"live", "sample", "realistic", "generated"}
        if self.DATA_SOURCE not in valid_sources:
            raise ValueError(f"DATA_SOURCE must be one of {valid_sources}, got '{self.DATA_SOURCE}'")

        valid_providers = {"anthropic", "openai"}
        if self.AI_PROVIDER not in valid_providers:
            raise ValueError(f"AI_PROVIDER must be one of {valid_providers}, got '{self.AI_PROVIDER}'")

        if self.AI_PROVIDER == "anthropic" and not self.ANTHROPIC_API_KEY:
            raise ValueError("AI_PROVIDER is 'anthropic' but ANTHROPIC_API_KEY is not set in .env")
        if self.AI_PROVIDER == "openai" and not self.OPENAI_API_KEY:
            raise ValueError("AI_PROVIDER is 'openai' but OPENAI_API_KEY is not set in .env")


settings = Settings()
