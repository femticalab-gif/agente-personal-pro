"""Esquema SQLite y migraciones.

Convención: las columnas en claro son solo metadatos necesarios para consultar y
programar (ids, fechas, estados). Todo contenido personal va en ``payload`` cifrado.
"""

from __future__ import annotations

MIGRATIONS: list[str] = [
    # v1
    """
    CREATE TABLE kv_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE kv_secure (
        key TEXT PRIMARY KEY,
        payload BLOB NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE oauth_tokens (
        provider TEXT PRIMARY KEY,
        payload BLOB NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE memory_facts (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        source TEXT NOT NULL,
        trusted INTEGER NOT NULL,
        payload BLOB NOT NULL
    );
    CREATE INDEX ix_memory_source ON memory_facts(source);
    CREATE TABLE notes (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        payload BLOB NOT NULL
    );
    CREATE TABLE tasks (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        status TEXT NOT NULL,
        priority INTEGER NOT NULL,
        due_at TEXT,
        payload BLOB NOT NULL
    );
    CREATE INDEX ix_tasks_status ON tasks(status, due_at);
    CREATE TABLE reminders (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        active INTEGER NOT NULL,
        next_run_at TEXT,
        recurrence TEXT,
        due_id TEXT,
        offset_days INTEGER,
        payload BLOB NOT NULL
    );
    CREATE INDEX ix_reminders_next ON reminders(active, next_run_at);
    CREATE TABLE contacts (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        email_index TEXT,
        payload BLOB NOT NULL
    );
    CREATE INDEX ix_contacts_email ON contacts(email_index);
    CREATE TABLE expenses (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        day TEXT NOT NULL,
        payload BLOB NOT NULL
    );
    CREATE INDEX ix_expenses_day ON expenses(day);
    CREATE TABLE loans (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        active INTEGER NOT NULL,
        payload BLOB NOT NULL
    );
    CREATE TABLE subscriptions (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        active INTEGER NOT NULL,
        payload BLOB NOT NULL
    );
    CREATE TABLE due_items (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        due_date TEXT NOT NULL,
        recurrence TEXT,
        kind TEXT NOT NULL,
        ref_id TEXT,
        active INTEGER NOT NULL,
        payload BLOB NOT NULL
    );
    CREATE INDEX ix_due_date ON due_items(active, due_date);
    CREATE TABLE budgets (
        month TEXT PRIMARY KEY,
        payload BLOB NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE health_appointments (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        at TEXT NOT NULL,
        status TEXT NOT NULL,
        payload BLOB NOT NULL
    );
    CREATE INDEX ix_appt_at ON health_appointments(at);
    CREATE TABLE treatments (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        active INTEGER NOT NULL,
        start_date TEXT NOT NULL,
        end_date TEXT,
        times TEXT NOT NULL,
        payload BLOB NOT NULL
    );
    CREATE TABLE med_intakes (
        id TEXT PRIMARY KEY,
        treatment_id TEXT NOT NULL,
        scheduled_at TEXT NOT NULL,
        status TEXT NOT NULL,
        recorded_at TEXT,
        UNIQUE(treatment_id, scheduled_at)
    );
    CREATE INDEX ix_intakes ON med_intakes(treatment_id, scheduled_at);
    CREATE TABLE lab_results (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        day TEXT NOT NULL,
        payload BLOB NOT NULL
    );
    CREATE TABLE vault_docs (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        doc_date TEXT,
        expires_at TEXT,
        status TEXT NOT NULL,
        size INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        payload BLOB NOT NULL
    );
    CREATE INDEX ix_vault_expires ON vault_docs(expires_at);
    CREATE TABLE fraud_flags (
        message_id TEXT PRIMARY KEY,
        thread_id TEXT,
        sender_index TEXT,
        created_at TEXT NOT NULL,
        payload BLOB NOT NULL
    );
    CREATE INDEX ix_fraud_thread ON fraud_flags(thread_id);
    CREATE INDEX ix_fraud_sender ON fraud_flags(sender_index);
    CREATE TABLE mail_seen (
        message_id TEXT PRIMARY KEY,
        seen_at TEXT NOT NULL,
        important INTEGER NOT NULL,
        digested INTEGER NOT NULL DEFAULT 0,
        payload BLOB
    );
    CREATE INDEX ix_mail_digest ON mail_seen(digested, seen_at);
    CREATE TABLE approvals (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        decided_at TEXT,
        status TEXT NOT NULL,
        tool TEXT NOT NULL,
        payload BLOB NOT NULL
    );
    CREATE TABLE api_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        day TEXT NOT NULL,
        month TEXT NOT NULL,
        model TEXT NOT NULL,
        kind TEXT NOT NULL,
        input_tokens INTEGER NOT NULL,
        output_tokens INTEGER NOT NULL,
        cost_usd REAL NOT NULL
    );
    CREATE INDEX ix_usage_day ON api_usage(day);
    CREATE INDEX ix_usage_month ON api_usage(month);
    CREATE TABLE notifications_sent (
        key TEXT PRIMARY KEY,
        sent_at TEXT NOT NULL
    );
    CREATE TABLE proposals (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        status TEXT NOT NULL,
        payload BLOB NOT NULL
    );
    CREATE TABLE session_entries (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        project_key TEXT NOT NULL,
        session_id TEXT NOT NULL,
        subpath TEXT NOT NULL DEFAULT '',
        uuid TEXT,
        created_at TEXT NOT NULL,
        payload BLOB NOT NULL
    );
    CREATE INDEX ix_session_key ON session_entries(project_key, session_id, subpath, seq);
    CREATE UNIQUE INDEX ux_session_uuid ON session_entries(project_key, session_id, subpath, uuid)
        WHERE uuid IS NOT NULL;
    CREATE TABLE session_meta (
        project_key TEXT NOT NULL,
        session_id TEXT NOT NULL,
        mtime INTEGER NOT NULL,
        summary BLOB,
        PRIMARY KEY (project_key, session_id)
    );
    CREATE TABLE chat_turns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        payload BLOB NOT NULL
    );
    CREATE INDEX ix_chat_turns ON chat_turns(session_id, id);
    CREATE TABLE chat_summaries (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        session_id TEXT NOT NULL,
        payload BLOB NOT NULL
    );
    """,
]

# Tablas con datos personales (para export y borrado total).
PERSONAL_TABLES: tuple[str, ...] = (
    "kv_secure",
    "memory_facts",
    "notes",
    "tasks",
    "reminders",
    "contacts",
    "expenses",
    "loans",
    "subscriptions",
    "due_items",
    "budgets",
    "health_appointments",
    "treatments",
    "med_intakes",
    "lab_results",
    "vault_docs",
    "fraud_flags",
    "mail_seen",
    "approvals",
    "proposals",
    "session_entries",
    "session_meta",
    "chat_turns",
    "chat_summaries",
    "notifications_sent",
)

# (tabla, columna) cifradas con Fernet (para rotación de clave). Se recorren por rowid.
ENCRYPTED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("kv_secure", "payload"),
    ("oauth_tokens", "payload"),
    ("memory_facts", "payload"),
    ("notes", "payload"),
    ("tasks", "payload"),
    ("reminders", "payload"),
    ("contacts", "payload"),
    ("expenses", "payload"),
    ("loans", "payload"),
    ("subscriptions", "payload"),
    ("due_items", "payload"),
    ("budgets", "payload"),
    ("health_appointments", "payload"),
    ("treatments", "payload"),
    ("lab_results", "payload"),
    ("vault_docs", "payload"),
    ("fraud_flags", "payload"),
    ("mail_seen", "payload"),
    ("approvals", "payload"),
    ("proposals", "payload"),
    ("session_entries", "payload"),
    ("session_meta", "summary"),
    ("chat_turns", "payload"),
    ("chat_summaries", "payload"),
)
