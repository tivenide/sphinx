"""Explicit runtime configuration; model weights never belong to the API image."""

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    database: str = "/data/sphinx.sqlite3"
    public_origin: str = "https://localhost:6500"
    additional_public_origins: tuple[str, ...] = ()
    cookie_secure: bool = True
    session_seconds: int = 12 * 60 * 60
    admin_username: str = "admin"
    admin_password_file: str = ""
    asr_url: str = "http://asr:8101/v1/audio/transcriptions"
    llm_url: str = "http://llm:8102/v1/chat/completions"
    tts_url: str = "http://tts:8103/v1/voice-clone/stream"
    service_token: str = ""
    llm_model: str = "sphinx-llm"
    asr_model: str = "Qwen/Qwen3-ASR-0.6B"
    tts_model: str = "sphinx-tts"
    speech_rate: float = 1.10
    request_timeout: float = 180.0
    max_record_seconds: int = 30
    model_control_url: str = ""
    model_control_token_file: str = ""
    ingestion_webhook_url: str = "http://n8n:5678/webhook/sphinx-ingest"
    ingestion_webhook_token_file: str = ""
    knowledge_db_host: str = "vector-db"
    knowledge_db_password_file: str = ""
    knowledge_ollama_url: str = "http://ollama.host:11434"
    knowledge_min_score: float = 0.45

    def __post_init__(self):
        for value in self.allowed_origins:
            origin = urlsplit(value)
            if (origin.scheme not in {"http", "https"} or not origin.hostname or origin.path
                    or origin.query or origin.fragment or origin.username is not None
                    or origin.password is not None or "*" in value or any(c.isspace() for c in value)):
                raise ValueError("Public origins must be exact origins, without credentials, paths or wildcards")
            origin.port  # Reject invalid or out-of-range ports at startup.
            if not self.cookie_secure and origin.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("Insecure cookies are only permitted on localhost")
        if not 0.5 <= self.speech_rate <= 2.0 or not 1 <= self.max_record_seconds <= 30:
            raise ValueError("Invalid voice limits")
        if not 60 <= self.session_seconds <= 7 * 24 * 3600:
            raise ValueError("Invalid session lifetime")
        if not 0 <= self.knowledge_min_score <= 1:
            raise ValueError("Invalid knowledge similarity threshold")

    @property
    def allowed_origins(self) -> frozenset[str]:
        return frozenset((self.public_origin, *self.additional_public_origins))

    @classmethod
    def from_env(cls):
        return cls(
            database=os.getenv("DATABASE_PATH", "/data/sphinx.sqlite3"),
            public_origin=os.getenv("PUBLIC_ORIGIN", "https://localhost:6500").rstrip("/"),
            additional_public_origins=tuple(
                value.rstrip("/") for value in os.getenv("ADDITIONAL_PUBLIC_ORIGINS", "").split()
            ),
            cookie_secure=os.getenv("COOKIE_SECURE", "true").lower() == "true",
            admin_username=os.getenv("ADMIN_USERNAME", "admin"),
            admin_password_file=os.getenv("ADMIN_PASSWORD_FILE", ""),
            asr_url=os.getenv("ASR_URL", cls.asr_url),
            llm_url=os.getenv("LLM_URL", cls.llm_url),
            tts_url=os.getenv("TTS_URL", cls.tts_url),
            service_token=os.getenv("SPHINX_SERVICE_TOKEN", ""),
            llm_model=os.getenv("LLM_MODEL", cls.llm_model),
            asr_model=os.getenv("ASR_MODEL", cls.asr_model),
            tts_model=os.getenv("TTS_MODEL", cls.tts_model),
            speech_rate=float(os.getenv("SPEECH_RATE", "1.10")),
            model_control_url=os.getenv("MODEL_CONTROL_URL", "").rstrip("/"),
            model_control_token_file=os.getenv("MODEL_CONTROL_TOKEN_FILE", ""),
            ingestion_webhook_url=os.getenv("INGESTION_WEBHOOK_URL", cls.ingestion_webhook_url),
            ingestion_webhook_token_file=os.getenv("INGESTION_WEBHOOK_TOKEN_FILE", ""),
            knowledge_db_host=os.getenv("KNOWLEDGE_DB_HOST", cls.knowledge_db_host),
            knowledge_db_password_file=os.getenv("KNOWLEDGE_DB_PASSWORD_FILE", ""),
            knowledge_ollama_url=os.getenv("KNOWLEDGE_OLLAMA_URL", cls.knowledge_ollama_url).rstrip("/"),
            knowledge_min_score=float(os.getenv("KNOWLEDGE_MIN_SCORE", "0.45")),
        )

    def initial_password(self) -> str | None:
        if not self.admin_password_file:
            return None
        return Path(self.admin_password_file).read_text().strip()
