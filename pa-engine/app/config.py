"""Environment. Secrets stay server-side (F7). Temperature and model ids are pinned (F8)."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

PIPELINE_VERSION = os.environ.get("PIPELINE_VERSION", "v4")
FHIR_BUILDER_VERSION = os.environ.get("FHIR_BUILDER_VERSION", "1")
SYNTH_GENERATOR_VERSION = os.environ.get("SYNTH_GENERATOR_VERSION", "1")


def _flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    @property
    def database_path(self) -> str:
        return os.environ.get("DATABASE_PATH", str(ROOT / "data" / "clearpath.db"))

    @property
    def storage_dir(self) -> Path:
        return Path(os.environ.get("STORAGE_DIR", str(ROOT / "data" / "storage")))

    @property
    def demo_dir(self) -> Path:
        return Path(os.environ.get("DEMO_DIR", str(ROOT / "data" / "demo")))

    @property
    def openai_api_key(self) -> str:
        return os.environ.get("OPENAI_API_KEY", "").strip()

    @property
    def openai_model(self) -> str:
        return os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")

    @property
    def openai_base_url(self) -> str:
        return os.environ.get("OPENAI_BASE_URL", "https://api.openai.com").rstrip("/")

    @property
    def judge_provider(self) -> str:
        return os.environ.get("JUDGE_PROVIDER", "gemini").strip().lower()

    @property
    def gemini_api_key(self) -> str:
        return os.environ.get("GEMINI_API_KEY", "").strip()

    @property
    def gemini_model(self) -> str:
        return os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

    @property
    def gemini_base_url(self) -> str:
        return os.environ.get(
            "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com"
        ).rstrip("/")

    @property
    def grok_api_key(self) -> str:
        return os.environ.get("GROK_API_KEY", "").strip()

    @property
    def grok_model(self) -> str:
        return os.environ.get("GROK_MODEL", "grok-3-mini")

    @property
    def grok_base_url(self) -> str:
        return os.environ.get("GROK_BASE_URL", "https://api.x.ai").rstrip("/")

    @property
    def timeout_extract(self) -> float:
        # Dense EOC chart pages often need >120s; 240s + one retry in llm.complete.
        return float(os.environ.get("TIMEOUT_EXTRACT_SECONDS", "240"))

    @property
    def timeout_judge(self) -> float:
        return float(os.environ.get("TIMEOUT_JUDGE_SECONDS", "60"))

    @property
    def timeout_quote(self) -> float:
        return float(os.environ.get("TIMEOUT_QUOTE_SECONDS", "20"))

    @property
    def max_concurrent_llm(self) -> int:
        return int(os.environ.get("MAX_CONCURRENT_LLM_CALLS", "4"))

    @property
    def context_group_pages(self) -> int:
        return int(os.environ.get("CONTEXT_GROUP_PAGES", "10"))

    @property
    def context_max_tokens(self) -> int:
        return int(os.environ.get("CONTEXT_MAX_TOKENS", "12000"))

    @property
    def working_memory_max_tokens(self) -> int:
        return int(os.environ.get("WORKING_MEMORY_MAX_TOKENS", "1500"))

    @property
    def section_page_cap(self) -> int:
        return int(os.environ.get("SECTION_PAGE_CAP", "40"))

    @property
    def run_max_model_calls(self) -> int:
        return int(os.environ.get("RUN_MAX_MODEL_CALLS", "400"))

    @property
    def run_max_seconds(self) -> int:
        return int(os.environ.get("RUN_MAX_SECONDS", "1200"))

    @property
    def demo_mode(self) -> bool:
        return _flag("DEMO_MODE")

    @property
    def demo_today(self) -> str:
        return os.environ.get("DEMO_TODAY", "2026-09-27")

    @property
    def insurer_request_info(self) -> bool:
        return _flag("INSURER_REQUEST_INFO")

    @property
    def insurer_delay_seconds(self) -> float:
        return float(os.environ.get("INSURER_DELAY_SECONDS", "5"))

    @property
    def fhir_base_url(self) -> str:
        return os.environ.get("FHIR_BASE_URL", "https://clearpath.example/fhir").rstrip("/")

    @property
    def allowed_origins(self) -> list[str]:
        raw = os.environ.get(
            "ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
        )
        return [p.strip() for p in raw.split(",") if p.strip()]

    @property
    def artifact_bucket(self) -> str:
        return os.environ.get("ARTIFACT_BUCKET", "artifacts")


settings = Settings()
