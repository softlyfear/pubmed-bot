"""Docker-манифесты: uv sync --frozen, volume SQLite, без Postgres. Без сборки образа."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_installs_via_uv_sync_frozen() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "uv sync --frozen" in text
    assert "pip install" not in text
    assert "pip3" not in text
    assert "python -m pip" not in text
    assert "uv.lock" in text
    assert "python:3.14" in text
    assert 'VOLUME ["/data"]' in text
    assert 'ENTRYPOINT ["python", "-m", "pubmed_bot.container"]' in text
    assert "USER app" not in text
    assert "chown -R app:app /app" not in text


def test_compose_bot_volume_no_postgres() -> None:
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    lowered = text.lower()
    assert "image: postgres" not in lowered
    assert "postgres:" not in lowered
    assert "database_url" not in lowered
    assert "asyncpg" not in lowered
    assert "postgres_user" not in lowered
    assert "postgres_password" not in lowered
    assert "postgres_db" not in lowered
    assert "env_file:" in text
    assert ".env" in text
    assert "SQLITE_PATH: /data/pubmed.db" in text
    assert "pubmed_data:/data" in text
    assert "no-new-privileges:true" in text
    assert "volumes:" in text
    assert "bot:" in text
    assert "pull_policy: build" in text


def test_dockerignore_excludes_env() -> None:
    text = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    lines = {line.strip() for line in text.splitlines()}
    assert ".env" in lines


def test_container_entrypoint_is_lf() -> None:
    raw = (ROOT / "src" / "pubmed_bot" / "container.py").read_bytes()
    assert b"\r" not in raw
    assert raw.startswith(b'"""')
    assert b"setgroups" in raw


def test_dockerfile_and_compose_are_lf() -> None:
    for name in ("Dockerfile", "docker-compose.yml"):
        raw = (ROOT / name).read_bytes()
        assert b"\r" not in raw, name
