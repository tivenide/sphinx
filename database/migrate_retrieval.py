#!/usr/bin/env python3
"""Add read-only retrieval to the existing knowledge database without resetting it."""

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    password = (ROOT / ".secrets/retrieval_db_password").read_text().strip()
    if len(password) < 32 or any(character in password for character in "\r\n\x00"):
        raise SystemExit("Invalid retrieval secret. Run scripts/setup_ingestion.py first.")
    migration = (ROOT / "database/retrieval.sql").read_text()
    result = subprocess.run(
        ["docker", "compose", "--project-directory", str(ROOT), "--env-file", str(ROOT / ".env"),
         "-f", str(ROOT / "compose.yaml"), "exec", "-T", "vector-db", "sh", "-eu", "-c",
         'IFS= read -r RETRIEVAL_DB_PASSWORD; export RETRIEVAL_DB_PASSWORD; '
         'exec psql --no-psqlrc --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" '
         '--quiet --set ON_ERROR_STOP=1'],
        input=password + "\n" + migration, capture_output=True, text=True,
    )
    if result.returncode:
        # SQL diagnostics can include source text and credentials. Do not echo them.
        raise SystemExit("Retrieval migration failed; transaction rolled back. Check database availability "
                         "and that all documents use the pinned embedding revision.")
    print("Retrieval role and pinned embedding revision ready; existing documents preserved.")


if __name__ == "__main__":
    try:
        main()
    except OSError:
        raise SystemExit("Migration could not start. Check local Docker and the retrieval secret file.") from None
