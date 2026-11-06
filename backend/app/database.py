"""Small SQLite store for accounts, sessions and import receipts, never file/audio content."""

import hashlib
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def public_user(row):
    return {"id": row["id"], "username": row["username"], "role": row["role"], "active": bool(row["active"])}


class Database:
    def __init__(self, path: str):
        self.path = path

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('admin','user')),
                    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
                CREATE TABLE IF NOT EXISTS ingestion_attempts (
                    id INTEGER PRIMARY KEY,
                    filename TEXT NOT NULL,
                    sha256 TEXT,
                    status TEXT NOT NULL CHECK(status IN
                        ('processing','ingested','already_exists','failed','unconfirmed')),
                    detail TEXT,
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    document_id TEXT,
                    chunks INTEGER
                );
                PRAGMA user_version=2;
            """)
            # n8n can continue after a backend restart; absence of a receipt is
            # not proof that the import failed.
            db.execute("""UPDATE ingestion_attempts SET status='unconfirmed', detail=?, finished_at=?
                WHERE status='processing'""", ("Verarbeitung durch einen Neustart unterbrochen; Ergebnis nicht bestätigt.", time.time()))

    def begin_ingestion(self, filename):
        with self.connection() as db:
            return db.execute("""INSERT INTO ingestion_attempts(filename,status,started_at)
                VALUES (?,'processing',?)""", (filename, time.time())).lastrowid

    def ingestion_hash(self, attempt_id, digest):
        with self.connection() as db:
            db.execute("UPDATE ingestion_attempts SET sha256=? WHERE id=?", (digest, attempt_id))

    def finish_ingestion(self, attempt_id, status, detail=None, document_id=None, chunks=None):
        with self.connection() as db:
            db.execute("""UPDATE ingestion_attempts
                SET status=?,detail=?,finished_at=?,document_id=?,chunks=? WHERE id=?""",
                (status, detail, time.time(), document_id, chunks, attempt_id))

    def list_ingestion(self, limit, offset):
        with self.connection() as db:
            db.execute("BEGIN")
            total = db.execute("SELECT count(*) FROM ingestion_attempts").fetchone()[0]
            rows = db.execute("SELECT * FROM ingestion_attempts ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))
            return {"items": [dict(row) for row in rows], "total": total, "offset": offset}

    def has_users(self):
        with self.connection() as db:
            return db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    def find_user(self, username: str):
        with self.connection() as db:
            row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            return dict(row) if row else None

    def get_user(self, user_id: int):
        with self.connection() as db:
            row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            return dict(row) if row else None

    def list_users(self):
        with self.connection() as db:
            return [public_user(row) for row in db.execute("SELECT * FROM users ORDER BY username")]

    def create_user(self, username, password_hash, role):
        with self.connection() as db:
            row_id = db.execute(
                "INSERT INTO users(username,password_hash,role) VALUES (?,?,?)",
                (username, password_hash, role),
            ).lastrowid
            return public_user(db.execute("SELECT * FROM users WHERE id=?", (row_id,)).fetchone())

    def update_user(self, user_id, *, active=None, role=None):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if user is None:
                raise LookupError
            new_active = bool(user["active"]) if active is None else active
            new_role = user["role"] if role is None else role
            if user["role"] == "admin" and user["active"] and (not new_active or new_role != "admin"):
                count = db.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1").fetchone()[0]
                if count <= 1:
                    raise ValueError("Mindestens ein aktiver Admin muss erhalten bleiben.")
            db.execute("UPDATE users SET active=?,role=? WHERE id=?", (new_active, new_role, user_id))
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            return public_user(db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())

    def set_password(self, user_id, password_hash):
        with self.connection() as db:
            changed = db.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id))
            if not changed.rowcount:
                raise LookupError
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def new_session(self, user_id: int, lifetime: int, expected_hash: str):
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            user = db.execute("SELECT active,password_hash FROM users WHERE id=?", (user_id,)).fetchone()
            if user is None or not user["active"] or user["password_hash"] != expected_hash:
                raise ValueError("Account changed during login")
            db.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
            # Keep at most five simultaneous logins per account.
            db.execute("""DELETE FROM sessions WHERE token_hash IN (
                SELECT token_hash FROM sessions WHERE user_id=? ORDER BY created_at DESC LIMIT -1 OFFSET 4
            )""", (user_id,))
            db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (token_digest(token), user_id, now + lifetime, now))
        return token

    def session(self, token: str):
        if not token or len(token) > 128:
            return None
        with self.connection() as db:
            row = db.execute("""SELECT users.*,sessions.expires_at,sessions.token_hash FROM sessions
                JOIN users ON users.id=sessions.user_id
                WHERE token_hash=? AND expires_at>? AND active=1""", (token_digest(token), time.time())).fetchone()
            return dict(row) if row else None

    def revoke_session(self, token: str):
        with self.connection() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (token_digest(token),))
