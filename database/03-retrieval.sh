#!/bin/sh
set -eu

# First initialization only. Existing databases use migrate_retrieval.py instead.
RETRIEVAL_DB_PASSWORD="$(cat /run/secrets/retrieval_db_password)"
if [ -z "$RETRIEVAL_DB_PASSWORD" ]; then
    echo 'retrieval_db_password must not be empty' >&2
    exit 1
fi
export RETRIEVAL_DB_PASSWORD
psql --no-psqlrc --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --quiet --set ON_ERROR_STOP=1 --file /opt/sphinx/retrieval.sql
unset RETRIEVAL_DB_PASSWORD
