#!/usr/bin/env python3
"""Validate the offline runtime and create local configuration without replacing secrets."""

import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def local_docker_socket():
    endpoint = os.getenv("DOCKER_HOST", "")
    if not endpoint:
        result = subprocess.run(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            capture_output=True, text=True, check=True,
        )
        endpoint = result.stdout.strip()
    if not endpoint.startswith("unix://"):
        raise SystemExit("Model control requires a local Docker Unix socket.")
    path = Path(endpoint.removeprefix("unix://"))
    if not path.is_socket():
        raise SystemExit("The local Docker socket was not found.")
    return str(path)


def has_setting(config, name):
    return any(
        line.strip().removeprefix("export ").partition("=")[0].strip() == name
        for line in config.splitlines() if not line.lstrip().startswith("#")
    )


def validate_runtime(env_path, docker_socket=None):
    environment = os.environ.copy()
    if docker_socket is not None:
        environment["MODEL_DOCKER_SOCKET"] = docker_socket
    result = subprocess.run(
        ["docker", "compose", "--project-directory", str(ROOT),
         "--env-file", str(env_path), "-f", str(ROOT / "compose.yaml"),
         "--profile", "models", "config", "--format", "json"],
        capture_output=True, text=True, check=True, env=environment,
    )
    config = json.loads(result.stdout)
    images = sorted({service["image"] for service in config["services"].values()})
    external_volumes = sorted({
        volume["name"] for volume in config.get("volumes", {}).values()
        if volume.get("external")
    })
    missing = []
    for resource, names in (("image", images), ("volume", external_volumes)):
        for name in names:
            inspected = subprocess.run(
                ["docker", resource, "inspect", name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if inspected.returncode:
                missing.append(f"{resource}: {name}")
    if missing:
        raise SystemExit("Missing offline prerequisites:\n  " + "\n  ".join(missing)
                         + "\nProvide them first; nothing was downloaded or created.")
    socket_path = next(
        mount["source"] for mount in config["services"]["model-control"]["volumes"]
        if mount["target"] == "/var/run/docker.sock"
    )
    if not Path(socket_path).is_socket():
        raise SystemExit("MODEL_DOCKER_SOCKET does not point to a local Docker Unix socket.")


def create_secret(path, length):
    if not path.exists():
        with path.open("x") as output:
            output.write(secrets.token_urlsafe(length) + "\n")
        # The private parent directory protects the host copy. The mounted file must
        # also be readable by the unprivileged application user inside the container.
        path.chmod(0o444)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check only; do not write configuration or secrets")
    args = parser.parse_args()
    env_path = ROOT / ".env"
    source = env_path if env_path.exists() else ROOT / ".env.example"
    config = source.read_text()
    docker_socket = None
    if not env_path.exists() or not has_setting(config, "MODEL_DOCKER_SOCKET"):
        docker_socket = local_docker_socket()
    validate_runtime(source, docker_socket)
    print("All configured images and external volumes are available locally.")
    print("Volume contents and GPU readiness are checked when the model services start.")
    if args.check:
        print("Check complete; no files changed.")
        return
    if not env_path.exists():
        config = config.replace("MODEL_DOCKER_SOCKET=/var/run/docker.sock", f"MODEL_DOCKER_SOCKET={docker_socket}")
        with env_path.open("x") as output:
            output.write(config)
        env_path.chmod(0o600)
        print(".env created.")
    elif docker_socket is not None:
        with env_path.open("a") as output:
            output.write(f"\nMODEL_DOCKER_SOCKET={docker_socket}\n")
        print("Local Docker socket configured; existing settings kept.")
    else:
        print(".env already exists; kept unchanged.")
    secret_dir = ROOT / ".secrets"
    secret_dir.mkdir(mode=0o700, exist_ok=True)
    create_secret(secret_dir / "admin_password", 24)
    create_secret(secret_dir / "model_control_token", 32)
    create_secret(secret_dir / "ingestion_webhook_token", 32)
    create_secret(secret_dir / "retrieval_db_password", 32)
    print("Bootstrap admin password: .secrets/admin_password (used only for an empty database).")
    print("Existing passwords and tokens were kept.")
    print("Prepare models: docker compose create --no-build --pull never asr llm tts")
    print("Start the app: docker compose up -d --no-build --pull never backend frontend model-control")


if __name__ == "__main__":
    try:
        main()
    except FileNotFoundError as exc:
        raise SystemExit(f"Required local file or command was not found: {exc.filename}") from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit("Docker configuration could not be read. Check the Docker context and Compose configuration.") from exc
