from __future__ import annotations

import sqlite3


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the durable Phase-C coordination schema idempotently."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS autonomous_comms_decision_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at REAL NOT NULL,
            decision TEXT NOT NULL,
            reason TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            thread_id INTEGER,
            source_note_id INTEGER,
            idempotency_key TEXT NOT NULL,
            metadata_json TEXT NOT NULL CHECK (json_valid(metadata_json))
        );
        CREATE INDEX IF NOT EXISTS idx_autonomous_comms_audit_correlation
            ON autonomous_comms_decision_audit(correlation_id, created_at);
        CREATE TRIGGER IF NOT EXISTS autonomous_comms_audit_no_update
        BEFORE UPDATE ON autonomous_comms_decision_audit BEGIN
            SELECT RAISE(ABORT, 'autonomous_comms_decision_audit is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS autonomous_comms_audit_no_delete
        BEFORE DELETE ON autonomous_comms_decision_audit BEGIN
            SELECT RAISE(ABORT, 'autonomous_comms_decision_audit is append-only');
        END;

        CREATE TABLE IF NOT EXISTS autonomous_comms_thread_claims (
            thread_id INTEGER PRIMARY KEY,
            owner_id TEXT NOT NULL CHECK (length(owner_id) > 0),
            lease_token TEXT NOT NULL UNIQUE CHECK (length(lease_token) >= 32),
            leased_until REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_autonomous_comms_claim_expiry
            ON autonomous_comms_thread_claims(leased_until);

        CREATE TABLE IF NOT EXISTS autonomous_comms_processing (
            thread_id INTEGER NOT NULL,
            source_note_id INTEGER NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('processing', 'sent', 'held', 'failed')),
            owner_token TEXT NOT NULL CHECK (length(owner_token) >= 16),
            outgoing_note_id INTEGER,
            updated_at REAL NOT NULL,
            PRIMARY KEY (thread_id, source_note_id),
            CHECK (state != 'sent' OR outgoing_note_id IS NOT NULL)
        );
        CREATE INDEX IF NOT EXISTS idx_autonomous_comms_processing_state
            ON autonomous_comms_processing(state, updated_at);
        CREATE TRIGGER IF NOT EXISTS autonomous_comms_sent_immutable
        BEFORE UPDATE ON autonomous_comms_processing
        WHEN OLD.state = 'sent' AND (
            NEW.state != OLD.state OR
            COALESCE(NEW.outgoing_note_id, -1) != COALESCE(OLD.outgoing_note_id, -1) OR
            NEW.owner_token != OLD.owner_token
        ) BEGIN
            SELECT RAISE(ABORT, 'sent idempotency record is immutable');
        END;

        CREATE TABLE IF NOT EXISTS autonomous_comms_daily_budget (
            day_utc TEXT PRIMARY KEY,
            replies_reserved INTEGER NOT NULL DEFAULT 0 CHECK (replies_reserved >= 0),
            tokens_reserved INTEGER NOT NULL DEFAULT 0 CHECK (tokens_reserved >= 0),
            new_threads_reserved INTEGER NOT NULL DEFAULT 0 CHECK (new_threads_reserved >= 0),
            in_flight INTEGER NOT NULL DEFAULT 0 CHECK (in_flight >= 0),
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS autonomous_comms_budget_reservations (
            reservation_id TEXT PRIMARY KEY,
            owner_token TEXT NOT NULL CHECK (length(owner_token) >= 16),
            day_utc TEXT NOT NULL,
            replies INTEGER NOT NULL CHECK (replies IN (0, 1)),
            estimated_tokens INTEGER NOT NULL CHECK (estimated_tokens >= 0),
            new_threads INTEGER NOT NULL CHECK (new_threads IN (0, 1)),
            state TEXT NOT NULL CHECK (state IN ('active', 'committed', 'refunded')),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY (day_utc) REFERENCES autonomous_comms_daily_budget(day_utc)
        );
        CREATE INDEX IF NOT EXISTS idx_autonomous_comms_reservations_state
            ON autonomous_comms_budget_reservations(state, day_utc);

        CREATE TABLE IF NOT EXISTS autonomous_comms_thread_state (
            thread_id INTEGER PRIMARY KEY,
            state TEXT NOT NULL CHECK (state IN ('open', 'closed', 'failed', 'poisoned', 'expired')),
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS autonomous_comms_promotion (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            approved INTEGER NOT NULL DEFAULT 0 CHECK (approved IN (0, 1)),
            approved_by TEXT,
            approved_at REAL,
            revoked_at REAL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS autonomous_comms_promotion_metrics (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            reviewed_count INTEGER NOT NULL DEFAULT 0 CHECK (reviewed_count >= 0),
            routing_correct_count INTEGER NOT NULL DEFAULT 0 CHECK (routing_correct_count >= 0),
            accepted_count INTEGER NOT NULL DEFAULT 0 CHECK (accepted_count >= 0),
            critical_violations INTEGER NOT NULL DEFAULT 0 CHECK (critical_violations >= 0),
            generation_total INTEGER NOT NULL DEFAULT 0 CHECK (generation_total >= 0),
            generation_failures INTEGER NOT NULL DEFAULT 0 CHECK (generation_failures >= 0),
            loop_blocks INTEGER NOT NULL DEFAULT 0 CHECK (loop_blocks >= 0),
            reviewed_at REAL,
            generation_updated_at REAL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS autonomous_comms_shadow_reviews (
            correlation_id TEXT PRIMARY KEY,
            reviewed_by TEXT NOT NULL,
            routing_correct INTEGER NOT NULL CHECK (routing_correct IN (0, 1)),
            accepted INTEGER NOT NULL CHECK (accepted IN (0, 1)),
            critical_violation INTEGER NOT NULL CHECK (critical_violation IN (0, 1)),
            reviewed_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS autonomous_comms_shadow_candidates (
            correlation_id TEXT PRIMARY KEY,
            thread_id INTEGER NOT NULL,
            source_note_id INTEGER NOT NULL,
            reply_text TEXT NOT NULL CHECK (length(reply_text) > 0),
            state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'reviewed')),
            created_at REAL NOT NULL,
            reviewed_at REAL,
            UNIQUE (thread_id, source_note_id)
        );
        CREATE INDEX IF NOT EXISTS idx_autonomous_comms_shadow_state
            ON autonomous_comms_shadow_candidates(state, created_at);
        """
    )
    conn.commit()
