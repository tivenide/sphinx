#!/bin/sh
set -eu

# Keep the password out of process arguments and let psql quote it as a literal.
INGESTION_DB_PASSWORD="$(cat /run/secrets/ingestion_db_password)"
if [ -z "$INGESTION_DB_PASSWORD" ]; then
    echo 'ingestion_db_password must not be empty' >&2
    exit 1
fi
export INGESTION_DB_PASSWORD

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --quiet --set ON_ERROR_STOP=1 <<'SQL'
\getenv ingestion_password INGESTION_DB_PASSWORD
BEGIN;
CREATE ROLE sphinx_ingest LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
    PASSWORD :'ingestion_password';
GRANT USAGE ON SCHEMA sphinx_ingestion TO sphinx_ingest;
GRANT SELECT, INSERT ON sphinx_ingestion.documents, sphinx_ingestion.document_chunks TO sphinx_ingest;
GRANT EXECUTE ON FUNCTION sphinx_ingestion.ingest_document(jsonb) TO sphinx_ingest;
COMMIT;
SQL

unset INGESTION_DB_PASSWORD
