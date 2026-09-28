"""Database roles for the trust boundary (spec section 3 item 6, design section 2).

opa_app    : api, worker, scheduler. Full access to application tables, no access to schema `broker`.
opa_broker : broker. Owns schema `broker`; reads only the public columns it needs for
             pre-dispatch checks, and updates action/approval state. It cannot read password
             hashes, conversation content, or task checkpoints.
Re-run after every migration (`opa db-roles`) so new tables receive grants.
"""
from __future__ import annotations

import psycopg
from psycopg import sql

BROKER_COLUMN_READS = {
    "owners": ["id", "preferences"],
    "tasks": ["id", "owner_id", "state"],
    "jobs": ["id", "task_id", "lease_token", "lease_expires_at"],
    # M4: attachment binding checks file identity and digests, never file names of other owners or text
    "artifacts": ["id", "owner_id", "name", "mime", "current_version", "deleted_at"],
    "artifact_versions": ["artifact_id", "version", "storage_key", "size", "sha256"],
}
BROKER_TABLE_READS = ["connections", "actions", "approvals"]
BROKER_UPDATES = {"actions": None, "approvals": None}
# The broker marks connections reauth_required/healthy; it cannot change modes or ownership.
BROKER_COLUMN_UPDATES = {"connections": ["status", "last_error", "last_health_at", "version", "updated_at"]}
BROKER_INSERTS = ["events", "notifications", "audit_log"]


def _plain_url(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://").replace("postgresql+psycopg://", "postgresql://")


def apply_roles(admin_url: str, app_password: str, broker_password: str) -> None:
    with psycopg.connect(_plain_url(admin_url), autocommit=True) as conn:
        for role, pw in (("opa_app", app_password), ("opa_broker", broker_password)):
            exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
            verb = "ALTER" if exists else "CREATE"
            conn.execute(sql.SQL(verb + " ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(pw)))
        dbname = conn.execute("SELECT current_database()").fetchone()[0]
        stmts = [
            sql.SQL("GRANT CONNECT ON DATABASE {} TO opa_app, opa_broker").format(sql.Identifier(dbname)),
            "GRANT USAGE ON SCHEMA public TO opa_app, opa_broker",
            # app: everything in public, nothing in broker
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO opa_app",
            "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO opa_app",
            "REVOKE ALL ON SCHEMA broker FROM PUBLIC",
            "REVOKE ALL ON SCHEMA broker FROM opa_app",
            "REVOKE ALL ON ALL TABLES IN SCHEMA broker FROM opa_app",
            # broker: its own schema
            "GRANT USAGE ON SCHEMA broker TO opa_broker",
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA broker TO opa_broker",
            # broker: start from nothing in public, then add the minimum
            "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM opa_broker",
        ]
        for table, cols in BROKER_COLUMN_READS.items():
            stmts.append(sql.SQL("GRANT SELECT ({}) ON {} TO opa_broker").format(sql.SQL(", ").join(map(sql.Identifier, cols)), sql.Identifier(table)))
        for table in BROKER_TABLE_READS:
            stmts.append(sql.SQL("GRANT SELECT ON {} TO opa_broker").format(sql.Identifier(table)))
        for table in BROKER_UPDATES:
            stmts.append(sql.SQL("GRANT UPDATE ON {} TO opa_broker").format(sql.Identifier(table)))
        for table, cols in BROKER_COLUMN_UPDATES.items():
            stmts.append(sql.SQL("GRANT UPDATE ({}) ON {} TO opa_broker").format(sql.SQL(", ").join(map(sql.Identifier, cols)), sql.Identifier(table)))
        for table in BROKER_INSERTS:
            stmts.append(sql.SQL("GRANT INSERT ON {} TO opa_broker").format(sql.Identifier(table)))
        stmts += [
            "GRANT USAGE, SELECT ON SEQUENCE events_id_seq, audit_log_id_seq TO opa_broker",
            # INSERT ... RETURNING reads back generated id and created_at; no content columns are readable
            "GRANT SELECT (id, created_at) ON events TO opa_broker",
            "GRANT SELECT (id, created_at) ON audit_log TO opa_broker",
            "GRANT SELECT (id, created_at) ON notifications TO opa_broker",
        ]
        for s in stmts:
            conn.execute(s)
