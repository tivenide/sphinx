#!/usr/bin/env python3
"""Test the real init hooks in an isolated, temporary pgvector container."""

import ast
import concurrent.futures
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid


ROOT = Path(__file__).resolve().parent
IMAGE = os.environ.get("PGVECTOR_TEST_IMAGE", "pgvector/pgvector@sha256:0dd3149a06e4579a0d70d8fef9945636cd514005e7237deb27f9b67886f8dc4f")
EMBEDDING_REVISION = "7907646426070047a77226ac3e684fbbe8410524f7b4a74d02837e43f2146bab"


def run(*args, **kwargs):
    return subprocess.run(args, text=True, capture_output=True, **kwargs)


def main():
    container = "sphinx-db-test-" + uuid.uuid4().hex[:12]
    # Exercise the exact production query without importing its HTTP/DB clients.
    knowledge = ast.parse((ROOT.parent / "backend/app/knowledge.py").read_text())
    search_sql = next(ast.literal_eval(node.value) for node in knowledge.body
                      if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "SEARCH_SQL"
                              for target in node.targets))

    def sql(query, *, admin=False, reader=False, ok=True):
        if admin:
            command = ["psql", "-U", "postgres", "-d", "sphinx_knowledge", "-v", "ON_ERROR_STOP=1", "-tA"]
        elif reader:
            command = ["sh", "-c", 'export PGPASSWORD="$(cat /run/secrets/retrieval_db_password)"; '
                       'exec psql -h 127.0.0.1 -U sphinx_retrieve -d sphinx_knowledge -v ON_ERROR_STOP=1 -tA']
        else:
            command = ["sh", "-c", 'export PGPASSWORD="$(cat /run/secrets/ingestion_db_password)"; '
                       'exec psql -h 127.0.0.1 -U sphinx_ingest -d sphinx_knowledge -v ON_ERROR_STOP=1 -tA']
        result = run("docker", "exec", "-i", container, *command, input=query)
        assert (result.returncode == 0) == ok, result.stderr[-2000:]
        return result.stdout.strip()

    def migrate(*, ok=True):
        result = run("docker", "exec", "-i", container, "sh", "-eu", "-c",
                     'IFS= read -r RETRIEVAL_DB_PASSWORD; export RETRIEVAL_DB_PASSWORD; '
                     'exec psql --no-psqlrc -U postgres -d sphinx_knowledge -q -v ON_ERROR_STOP=1',
                     input=secret.read_text().strip() + "\n" + (ROOT / "retrieval.sql").read_text())
        assert (result.returncode == 0) == ok, result.stderr[-2000:]

    def call(payload):
        literal = json.dumps(payload, allow_nan=False).replace("'", "''")
        return f"SELECT sphinx_ingestion.ingest_document('{literal}'::jsonb);"

    def counts():
        return sql("SELECT (SELECT count(*) FROM sphinx_ingestion.documents), "
                   "(SELECT count(*) FROM sphinx_ingestion.document_chunks);")

    with tempfile.TemporaryDirectory(prefix="sphinx-db-test-") as temporary:
        secret = Path(temporary) / "password"
        # Quotes/backslashes exercise psql literal quoting; this is disposable test data.
        secret.write_text("isolated-test-'password\\with$symbols\n")
        secret.chmod(0o444)
        try:
            result = run(
                "docker", "run", "--pull", "never", "--detach", "--rm", "--name", container,
                "--network", "none", "--tmpfs", "/var/lib/postgresql/data:rw",
                "--env", "POSTGRES_DB=sphinx_knowledge", "--env", "POSTGRES_USER=postgres",
                "--env", "POSTGRES_PASSWORD_FILE=/run/secrets/vector_db_admin_password",
                "--mount", f"type=bind,src={secret},dst=/run/secrets/vector_db_admin_password,readonly",
                "--mount", f"type=bind,src={secret},dst=/run/secrets/ingestion_db_password,readonly",
                "--mount", f"type=bind,src={secret},dst=/run/secrets/retrieval_db_password,readonly",
                "--mount", f"type=bind,src={ROOT / '01-schema.sql'},dst=/docker-entrypoint-initdb.d/01-schema.sql,readonly",
                "--mount", f"type=bind,src={ROOT / '02-role.sh'},dst=/docker-entrypoint-initdb.d/02-role.sh,readonly",
                "--mount", f"type=bind,src={ROOT / '03-retrieval.sh'},dst=/docker-entrypoint-initdb.d/03-retrieval.sh,readonly",
                "--mount", f"type=bind,src={ROOT / 'retrieval.sql'},dst=/opt/sphinx/retrieval.sql,readonly",
                IMAGE,
            )
            assert result.returncode == 0, result.stderr
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                ready = run("docker", "exec", container, "sh", "-c",
                            'test "$(cat /proc/1/comm)" = postgres && '
                            'pg_isready -U postgres -d sphinx_knowledge >/dev/null')
                if ready.returncode == 0:
                    break
                time.sleep(0.25)
            else:
                raise AssertionError("Temporary PostgreSQL did not finish initialization")

            payload = {
                "sha256": "a" * 64, "filename": "Report's.pdf",
                "embedding_model": "bge-m3:latest", "embedding_revision": EMBEDDING_REVISION,
                "chunks": [{"index": i, "text": f"Example section {i}",
                            "embedding": [1.0 if position == i else 0.0 for position in range(1024)]}
                           for i in range(2)],
            }
            first = json.loads(sql(call(payload)))
            again = json.loads(sql(call(payload)))
            assert first["status"] == "ingested" and first["chunks"] == 2
            assert again["status"] == "already_exists" and again["document_id"] == first["document_id"]
            assert counts() == "1|2"
            print("PASS: init hooks, password authentication, ingestion and idempotency")

            invalid = [None, {}]
            for key, value in [("sha256", "X" * 64), ("filename", "\n"), ("filename", "x" * 256),
                               ("embedding_model", "other"), ("embedding_revision", "different"),
                               ("embedding_revision", ""), ("chunks", []), ("chunks", {}),
                               ("chunks", [{}] * 1001)]:
                item = copy.deepcopy(payload)
                item[key] = value
                invalid.append(item)
            for key, value in [("index", 1), ("index", None), ("text", "\n\t"), ("text", "x" * 1001),
                               ("embedding", [0.01] * 768), ("embedding", [0.01] * 1023 + ["0.1"]),
                               ("embedding", [0.01] * 1023 + [1e300])]:
                item = copy.deepcopy(payload)
                item["chunks"][0][key] = value
                invalid.append(item)
            for item in invalid:
                sql(call(item), ok=False)
            assert counts() == "1|2"
            print(f"PASS: {len(invalid)} invalid payloads rejected without writes")

            incompatible = copy.deepcopy(payload)
            incompatible.update(sha256="d" * 64, embedding_revision="0" * 64)
            sql(call(incompatible), ok=False)
            assert counts() == "1|2"
            migrate()
            migrate()
            assert counts() == "1|2"
            # Simulate an existing database from before the revision constraint.
            sql("ALTER TABLE sphinx_ingestion.documents DROP CONSTRAINT documents_embedding_revision_pinned; "
                "UPDATE sphinx_ingestion.documents SET embedding_revision = 'incompatible';", admin=True)
            migrate(ok=False)
            assert sql("SELECT embedding_revision FROM sphinx_ingestion.documents;") == "incompatible"
            assert counts() == "1|2"
            sql("UPDATE sphinx_ingestion.documents SET embedding_revision = '" + EMBEDDING_REVISION + "';", admin=True)
            migrate()
            assert counts() == "1|2"
            print("PASS: repeated migration preserves documents and rejects incompatible revisions atomically")

            sql("""CREATE FUNCTION public.fail_second_chunk() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN IF NEW.chunk_index = 1 THEN RAISE EXCEPTION 'forced insert failure'; END IF;
                RETURN NEW; END $$;
                CREATE TRIGGER fail_second_chunk BEFORE INSERT ON sphinx_ingestion.document_chunks
                FOR EACH ROW EXECUTE FUNCTION public.fail_second_chunk();""", admin=True)
            failed = copy.deepcopy(payload)
            failed["sha256"] = "b" * 64
            sql(call(failed), ok=False)
            assert counts() == "1|2"
            sql("DROP TRIGGER fail_second_chunk ON sphinx_ingestion.document_chunks; "
                "DROP FUNCTION public.fail_second_chunk();", admin=True)
            print("PASS: second-chunk failure rolls back document and all chunks")

            parallel_payload = copy.deepcopy(payload)
            parallel_payload["sha256"] = "c" * 64
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                first_write = pool.submit(sql, "SET application_name = 'sphinx-concurrency-test'; BEGIN; "
                                          + call(parallel_payload) + " SELECT pg_sleep(2); COMMIT;")
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if sql("SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE "
                           "application_name = 'sphinx-concurrency-test' AND wait_event = 'PgSleep');",
                           admin=True) == "t":
                        break
                    time.sleep(0.05)
                else:
                    raise AssertionError("First import did not reach its uncommitted state")
                second_write = pool.submit(sql, call(parallel_payload))
                assert '"status": "ingested"' in first_write.result()
                assert json.loads(second_write.result())["status"] == "already_exists"
            assert counts() == "2|4"
            print("PASS: concurrent imports produce one document and one set of chunks")

            sql("DELETE FROM sphinx_ingestion.documents;", ok=False)
            sql("UPDATE sphinx_ingestion.documents SET filename = 'changed';", ok=False)
            sql("CREATE TABLE sphinx_ingestion.unwanted (id integer);", ok=False)
            sql("CREATE ROLE unwanted;", ok=False)
            assert sql("SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication "
                       "FROM pg_roles WHERE rolname = current_user;") == "f"
            assert counts() == "2|4"
            print("PASS: writer cannot delete, update, alter the schema or create roles")

            assert sql("SELECT count(*) FROM sphinx_ingestion.document_chunks;", reader=True) == "4"
            assert sql("SHOW default_transaction_read_only;", reader=True) == "on"
            assert sql("SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls "
                       "FROM pg_roles WHERE rolname = current_user;", reader=True) == "f"
            # Table/function permissions remain read-only even if the client changes
            # its transaction default; there is no write credential in the backend.
            for query in (
                "INSERT INTO sphinx_ingestion.documents SELECT * FROM sphinx_ingestion.documents;",
                "UPDATE sphinx_ingestion.documents SET filename = 'changed';",
                "DELETE FROM sphinx_ingestion.document_chunks;",
                "TRUNCATE sphinx_ingestion.document_chunks;",
                "CREATE TABLE sphinx_ingestion.unwanted (id integer);",
                "CREATE ROLE unwanted;",
                call(payload),
            ):
                sql("SET default_transaction_read_only = off; " + query, reader=True, ok=False)
            assert counts() == "2|4"
            print("PASS: retrieval login can read but cannot write, ingest or administer the database")

            def search(revision=EMBEDDING_REVISION):
                query_vector = json.dumps(payload["chunks"][0]["embedding"])
                return sql("\\set QUIET on\nPREPARE knowledge_search(text, text, text) AS " + search_sql
                           + ";\nEXECUTE knowledge_search('" + query_vector + "', 'bge-m3:latest', '"
                           + revision + "');", reader=True)

            hits = [line.split("|") for line in search().splitlines()]
            assert len(hits) == 4 and all(len(hit) == 5 for hit in hits), hits
            assert first["document_id"] in {hit[0] for hit in hits}
            assert len({hit[0] for hit in hits}) == 2
            # Matching unit vectors sort before orthogonal sections; the SELECT
            # returns the exact source filename, chunk index and text for grounding.
            for hit, index, expected_score in zip(hits, [0, 0, 1, 1], [1.0, 1.0, 0.0, 0.0], strict=True):
                assert hit[1:4] == ["Report's.pdf", str(index), f"Example section {index}"], hit
                assert abs(float(hit[4]) - expected_score) < 1e-6, hit
            assert search("0" * 64) == ""
            print("PASS: production cosine query returns ranked sources with the pinned revision filter")

            dump = subprocess.run(["docker", "exec", container, "pg_dump", "-U", "postgres",
                                   "-d", "sphinx_knowledge", "-Fc"], capture_output=True, check=True)
            sql("CREATE DATABASE sphinx_restore;", admin=True)
            restore = subprocess.run(["docker", "exec", "-i", container, "pg_restore", "-U", "postgres",
                                      "-d", "sphinx_restore", "--single-transaction", "--exit-on-error"],
                                     input=dump.stdout, capture_output=True)
            assert restore.returncode == 0, restore.stderr.decode()
            restored_count = run("docker", "exec", container, "psql", "-U", "postgres", "-d", "sphinx_restore",
                                 "-tAc", "SELECT (SELECT count(*) FROM sphinx_ingestion.documents), "
                                 "(SELECT count(*) FROM sphinx_ingestion.document_chunks);")
            assert restored_count.returncode == 0 and restored_count.stdout.strip() == "2|4"
            print("PASS: custom-format backup restores schema and data into a new database")
        finally:
            run("docker", "rm", "--force", container)


if __name__ == "__main__":
    main()
