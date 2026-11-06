-- Shared by the first-init hook and the explicit migration helper.
-- The helper supplies the password through stdin/environment, never argv.
\getenv retrieval_password RETRIEVAL_DB_PASSWORD
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

-- Revalidate all existing rows, even on repeated migration. An incompatible
-- document aborts this transaction; no document, chunk or credential is changed.
ALTER TABLE sphinx_ingestion.documents
    DROP CONSTRAINT IF EXISTS documents_embedding_revision_pinned;
ALTER TABLE sphinx_ingestion.documents
    ADD CONSTRAINT documents_embedding_revision_pinned CHECK (
        embedding_revision = '7907646426070047a77226ac3e684fbbe8410524f7b4a74d02837e43f2146bab'
    );

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sphinx_retrieve') THEN
        CREATE ROLE sphinx_retrieve;
    END IF;
END $$;
ALTER ROLE sphinx_retrieve WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD :'retrieval_password';
ALTER ROLE sphinx_retrieve SET default_transaction_read_only = on;
REVOKE ALL ON SCHEMA sphinx_ingestion FROM sphinx_retrieve;
REVOKE ALL ON ALL TABLES IN SCHEMA sphinx_ingestion FROM sphinx_retrieve;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA sphinx_ingestion FROM sphinx_retrieve;
GRANT USAGE ON SCHEMA sphinx_ingestion TO sphinx_retrieve;
GRANT SELECT ON sphinx_ingestion.documents, sphinx_ingestion.document_chunks TO sphinx_retrieve;
COMMIT;
