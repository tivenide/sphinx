#!/usr/bin/env python3
"""Import missing Sphinx credentials and a draft workflow into the project n8n."""

import json
from pathlib import Path
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ["docker", "compose", "--project-directory", str(ROOT),
           "--env-file", str(ROOT / ".env"), "-f", str(ROOT / "compose.yaml")]
HEADER_ID = "e8b9b7f1-615a-4b4c-8a82-7c008143d101"
POSTGRES_ID = "e8b9b7f1-615a-4b4c-8a82-7c008143d102"


def execute(*command, data=None):
    result = subprocess.run(COMPOSE + ["exec", "-T", "n8n", *command],
                            input=data, capture_output=True, text=True)
    if result.returncode:
        # Do not echo CLI diagnostics that might include credential input.
        raise SystemExit("n8n import failed. Confirm that the project's n8n is healthy and its secrets match.")
    return result.stdout


def secret(name):
    value = (ROOT / ".secrets" / name).read_text().strip()
    if not value:
        raise SystemExit(f"Secret file {name} is empty.")
    return value


def import_json(kind, value):
    path = "/tmp/sphinx-import-" + uuid.uuid4().hex + ".json"
    try:
        execute("node", "-e",
                "const fs=require('fs');fs.writeFileSync(process.argv[1],fs.readFileSync(0),{mode:0o600,flag:'wx'});",
                path, data=json.dumps(value))
        execute("n8n", "import:" + kind, "--input=" + path)
    finally:
        execute("node", "-e", "require('fs').rmSync(process.argv[1],{force:true})", path)


def main():
    # Read only IDs, not credential data. These tables belong to the pinned n8n image.
    state = json.loads(execute("node", "--disable-warning=ExperimentalWarning", "-e", """
const {DatabaseSync}=require('node:sqlite');
const db=new DatabaseSync('/home/node/.n8n/database.sqlite',{readOnly:true});
console.log(JSON.stringify({
  credentials:db.prepare('SELECT id FROM credentials_entity').all().map(x=>x.id),
  workflows:db.prepare('SELECT id FROM workflow_entity').all().map(x=>x.id)
}));
db.close();
"""))
    credentials = [
        {"id": HEADER_ID, "name": "Sphinx Ingestion Header", "type": "httpHeaderAuth",
         "data": {"name": "X-Ingestion-Token", "value": secret("ingestion_webhook_token")}},
        {"id": POSTGRES_ID, "name": "Sphinx Knowledge DB", "type": "postgres",
         "data": {"host": "vector-db", "port": 5432, "database": "sphinx_knowledge",
                  "user": "sphinx_ingest", "password": secret("ingestion_db_password"),
                  "ssl": "disable", "sshTunnel": False}},
    ]
    missing = [entry for entry in credentials if entry["id"] not in state["credentials"]]
    if missing:
        import_json("credentials", missing)
    workflow = json.loads((ROOT / "n8n/pdf-ingestion.workflow.json").read_text())
    if workflow.get("active"):
        raise SystemExit("The repository workflow must be an inactive draft.")
    if workflow["id"] not in state["workflows"]:
        import_json("workflow", [workflow])
        print("Sphinx ingestion workflow imported as an inactive draft.")
    else:
        print("Existing Sphinx workflow kept; no manual edits overwritten.")
    print(f"Created {len(missing)} missing credentials; existing credentials kept.")
    print("Finish the n8n owner setup in the browser, review the workflow, then publish it.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        raise SystemExit("Import failed. Run setup_ingestion.py and start the project n8n first.") from error
