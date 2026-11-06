#!/usr/bin/env python3
"""Prepare the optional ingestion services without downloading or replacing data."""

import argparse
import json
from pathlib import Path
import subprocess

from setup import create_secret, local_docker_socket

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("vector-db", "n8n", "n8n-runner")
SECRETS = (
    "vector_db_admin_password", "ingestion_db_password", "n8n_encryption_key",
    "n8n_runner_token", "ingestion_webhook_token", "retrieval_db_password",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate only; create no files")
    args = parser.parse_args()
    env = ROOT / ".env"
    source = env if env.exists() else ROOT / ".env.example"
    result = subprocess.run(
        ["docker", "compose", "--project-directory", str(ROOT), "--env-file", str(source),
         "-f", str(ROOT / "compose.yaml"), "--profile", "ingestion", "config", "--format", "json"],
        check=True, capture_output=True, text=True,
    )
    config = json.loads(result.stdout)
    missing = []
    for name in SERVICES:
        image = config["services"][name]["image"]
        if subprocess.run(["docker", "image", "inspect", image],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            missing.append(image)
    if missing:
        raise SystemExit("Provide these pinned images first; setup downloads nothing:\n  "
                         + "\n  ".join(missing))
    if args.check:
        absent = [name for name in SECRETS if not (ROOT / ".secrets" / name).is_file()]
        if absent:
            raise SystemExit("Run setup_ingestion.py to create missing secret files: " + ", ".join(absent))
        print("Ingestion images and secret files are available. No changes made.")
        return
    if not env.exists():
        content = source.read_text().replace(
            "MODEL_DOCKER_SOCKET=/var/run/docker.sock", f"MODEL_DOCKER_SOCKET={local_docker_socket()}")
        with env.open("x") as output:
            output.write(content)
        env.chmod(0o600)
    secret_dir = ROOT / ".secrets"
    secret_dir.mkdir(mode=0o700, exist_ok=True)
    for name in SECRETS:
        create_secret(secret_dir / name, 32)
    print("Ingestion secrets ready; existing files and .env kept.")
    print("Start: docker compose up -d --no-build --pull never vector-db n8n n8n-runner")
    print("Import draft: python3 scripts/import_ingestion.py")
    print("Existing database: python3 database/migrate_retrieval.py (preserves stored documents)")


if __name__ == "__main__":
    try:
        main()
    except (OSError, subprocess.CalledProcessError) as error:
        raise SystemExit("Ingestion setup failed. Check local Docker and configuration files.") from error
