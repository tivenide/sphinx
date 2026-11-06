-- Applies to an empty sphinx_knowledge database through the image's init hook.
BEGIN;

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
CREATE SCHEMA IF NOT EXISTS sphinx_ingestion;
REVOKE ALL ON SCHEMA sphinx_ingestion FROM PUBLIC;

CREATE TABLE sphinx_ingestion.documents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    sha256 text NOT NULL UNIQUE CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    filename text NOT NULL CHECK (length(filename) <= 255 AND filename ~ '[^[:space:]]'),
    embedding_model text NOT NULL CHECK (embedding_model = 'bge-m3:latest'),
    embedding_revision text NOT NULL CHECK (length(embedding_revision) <= 200 AND embedding_revision ~ '[^[:space:]]'),
    chunk_count integer NOT NULL CHECK (chunk_count BETWEEN 1 AND 1000),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT documents_embedding_revision_pinned CHECK (
        embedding_revision = '7907646426070047a77226ac3e684fbbe8410524f7b4a74d02837e43f2146bab'
    )
);

CREATE TABLE sphinx_ingestion.document_chunks (
    document_id uuid NOT NULL REFERENCES sphinx_ingestion.documents(id) ON DELETE CASCADE,
    chunk_index integer NOT NULL CHECK (chunk_index BETWEEN 0 AND 999),
    content text NOT NULL CHECK (length(content) <= 1000 AND content ~ '[^[:space:]]'),
    embedding public.vector(1024) NOT NULL,
    PRIMARY KEY (document_id, chunk_index)
);

-- One call is one transaction; any failure rolls back the document and all chunks.
-- This function uses the caller's restricted table privileges (SECURITY INVOKER).
CREATE FUNCTION sphinx_ingestion.ingest_document(payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = pg_catalog, public, sphinx_ingestion
AS $$
DECLARE
    document sphinx_ingestion.documents%ROWTYPE;
    chunk jsonb;
    ordinal bigint;
    total integer;
BEGIN
    IF jsonb_typeof(payload) IS DISTINCT FROM 'object'
       OR jsonb_typeof(payload->'sha256') IS DISTINCT FROM 'string'
       OR (payload->>'sha256') !~ '^[0-9a-f]{64}$'
       OR jsonb_typeof(payload->'filename') IS DISTINCT FROM 'string'
       OR length(payload->>'filename') > 255
       OR (payload->>'filename') !~ '[^[:space:]]'
       OR jsonb_typeof(payload->'embedding_model') IS DISTINCT FROM 'string'
       OR payload->>'embedding_model' <> 'bge-m3:latest'
       OR jsonb_typeof(payload->'embedding_revision') IS DISTINCT FROM 'string'
       OR length(payload->>'embedding_revision') > 200
       OR (payload->>'embedding_revision') !~ '[^[:space:]]'
       OR jsonb_typeof(payload->'chunks') IS DISTINCT FROM 'array'
    THEN
        RAISE EXCEPTION 'Invalid document metadata' USING ERRCODE = '22023';
    END IF;

    total := jsonb_array_length(payload->'chunks');
    IF total NOT BETWEEN 1 AND 1000 THEN
        RAISE EXCEPTION 'Expected 1 to 1000 chunks' USING ERRCODE = '22023';
    END IF;

    FOR chunk, ordinal IN
        SELECT value, ordinality
        FROM jsonb_array_elements(payload->'chunks') WITH ORDINALITY
    LOOP
        IF jsonb_typeof(chunk) IS DISTINCT FROM 'object'
           OR jsonb_typeof(chunk->'index') IS DISTINCT FROM 'number'
           OR jsonb_typeof(chunk->'text') IS DISTINCT FROM 'string'
           OR (chunk->>'text') !~ '[^[:space:]]'
           OR length(chunk->>'text') > 1000
           OR jsonb_typeof(chunk->'embedding') IS DISTINCT FROM 'array'
        THEN
            RAISE EXCEPTION 'Invalid chunk at index %', ordinal - 1 USING ERRCODE = '22023';
        END IF;
        IF (chunk->>'index')::numeric <> ordinal - 1
           OR jsonb_array_length(chunk->'embedding') <> 1024
           OR EXISTS (
               SELECT 1 FROM jsonb_array_elements(chunk->'embedding') AS element(value)
               WHERE jsonb_typeof(value) <> 'number'
           )
        THEN
            RAISE EXCEPTION 'Invalid chunk index or embedding at index %', ordinal - 1 USING ERRCODE = '22023';
        END IF;
        -- pgvector also rejects non-finite and out-of-range float values.
        PERFORM (chunk->'embedding')::text::public.vector(1024);
    END LOOP;

    INSERT INTO sphinx_ingestion.documents
        (sha256, filename, embedding_model, embedding_revision, chunk_count)
    VALUES
        (payload->>'sha256', payload->>'filename', payload->>'embedding_model',
         payload->>'embedding_revision', total)
    ON CONFLICT (sha256) DO NOTHING
    RETURNING * INTO document;

    IF NOT FOUND THEN
        SELECT * INTO STRICT document
        FROM sphinx_ingestion.documents
        WHERE sha256 = payload->>'sha256';
        IF document.embedding_model <> payload->>'embedding_model'
           OR document.embedding_revision <> payload->>'embedding_revision'
        THEN
            RAISE EXCEPTION 'Document already uses a different embedding model or revision'
                USING ERRCODE = '22023';
        END IF;
        RETURN jsonb_build_object('status', 'already_exists', 'document_id', document.id,
                                  'chunks', document.chunk_count);
    END IF;

    INSERT INTO sphinx_ingestion.document_chunks
        (document_id, chunk_index, content, embedding)
    SELECT document.id, (ordinality - 1)::integer, value->>'text',
           (value->'embedding')::text::public.vector(1024)
    FROM jsonb_array_elements(payload->'chunks') WITH ORDINALITY;

    RETURN jsonb_build_object('status', 'ingested', 'document_id', document.id,
                              'chunks', total);
END;
$$;

REVOKE ALL ON FUNCTION sphinx_ingestion.ingest_document(jsonb) FROM PUBLIC;
COMMIT;
