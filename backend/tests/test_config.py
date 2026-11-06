import pytest

from app.config import Settings


def test_additional_origins_from_environment(monkeypatch):
    monkeypatch.setenv("PUBLIC_ORIGIN", "https://server.example:6500")
    monkeypatch.setenv("ADDITIONAL_PUBLIC_ORIGINS", " https://192.168.1.10:6500/ https://lan.example:6500 ")
    assert Settings.from_env().allowed_origins == {
        "https://server.example:6500", "https://192.168.1.10:6500", "https://lan.example:6500",
    }


@pytest.mark.parametrize("origin", [
    "*", "https://*.example", "https://lan.example/path", "https://lan.example?query=1",
    "https://lan.example#fragment", "https://user:password@lan.example", "https://lan.example:99999",
    "https://lan.example:invalid", "https://lan.example https://other.example", "null", "",
])
def test_invalid_additional_origins_rejected(origin):
    with pytest.raises(ValueError):
        Settings(additional_public_origins=(origin,))


def test_additional_lan_origin_cannot_enable_insecure_cookies():
    with pytest.raises(ValueError, match="Insecure cookies"):
        Settings(cookie_secure=False, additional_public_origins=("https://192.168.1.10:6500",))


@pytest.mark.parametrize("score", [-0.1, 1.1, float("nan"), float("inf")])
def test_invalid_knowledge_threshold_rejected(score):
    with pytest.raises(ValueError, match="knowledge"):
        Settings(knowledge_min_score=score)


def test_knowledge_settings_from_environment(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_DB_HOST", "knowledge-db")
    monkeypatch.setenv("KNOWLEDGE_DB_PASSWORD_FILE", "/run/secrets/retrieval_db_password")
    monkeypatch.setenv("KNOWLEDGE_OLLAMA_URL", "http://ollama:11434/")
    monkeypatch.setenv("KNOWLEDGE_MIN_SCORE", "0.5")
    settings = Settings.from_env()
    assert settings.knowledge_db_host == "knowledge-db"
    assert settings.knowledge_db_password_file == "/run/secrets/retrieval_db_password"
    assert settings.knowledge_ollama_url == "http://ollama:11434"
    assert settings.knowledge_min_score == 0.5
