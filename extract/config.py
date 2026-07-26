"""Centralized configuration loaded from environment / .env file.

Import `settings` anywhere you need an API key or path:

    from extract.config import settings
    key = settings.congress_api_key
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (one level above this package) if present.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    """Immutable view of runtime configuration."""

    congress_api_key: str
    fec_api_key: str
    lda_api_key: str
    data_dir: Path
    db_path: Path

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.getenv("DATA_DIR", "data"))
        if not data_dir.is_absolute():
            data_dir = _PROJECT_ROOT / data_dir
        db_path = Path(os.getenv("DB_PATH", str(data_dir / "irs990_full.db")))
        if not db_path.is_absolute():
            db_path = _PROJECT_ROOT / db_path

        data_dir.mkdir(parents=True, exist_ok=True)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        return cls(
            congress_api_key=os.getenv("CONGRESS_API_KEY", "").strip(),
            fec_api_key=os.getenv("FEC_API_KEY", "DEMO_KEY").strip(),
            lda_api_key=os.getenv("LDA_API_KEY", "").strip(),
            data_dir=data_dir,
            db_path=db_path,
        )

    def require(self, attr: str) -> str:
        """Return a key, raising a clear error if it is missing."""
        value = getattr(self, attr)
        if not value:
            raise RuntimeError(
                f"Missing required config '{attr}'. "
                f"Set it in your .env file (see .env.example)."
            )
        return value


settings = Settings.from_env()
