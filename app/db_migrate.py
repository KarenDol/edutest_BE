import re
import secrets

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import User


def _rebuild_users_email_nullable(conn) -> None:
    """SQLite cannot drop NOT NULL in place: recreate users with a nullable email, keeping rows and indexes."""
    if conn.execute(text("PRAGMA foreign_keys")).scalar():
        # DROP TABLE would cascade into submissions etc.
        raise RuntimeError("migrate_users: refusing to rebuild users while PRAGMA foreign_keys is ON")
    create_sql = conn.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'")).scalar()
    new_sql, n = re.subn(r"\bemail VARCHAR\(255\) NOT NULL", "email VARCHAR(255)", create_sql)
    if n != 1:
        raise RuntimeError("migrate_users: unexpected users schema, cannot make email nullable")
    new_sql = new_sql.replace("CREATE TABLE users", "CREATE TABLE users__new", 1)
    index_sqls = [
        r[0]
        for r in conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='users' AND sql IS NOT NULL")
        ).fetchall()
    ]
    conn.execute(text(new_sql))
    conn.execute(text("INSERT INTO users__new SELECT * FROM users"))
    conn.execute(text("DROP TABLE users"))
    conn.execute(text("ALTER TABLE users__new RENAME TO users"))
    for stmt in index_sqls:
        conn.execute(text(stmt))


def migrate_users(engine) -> None:
    """SQLite: add new user columns if missing; backfill legacy rows."""
    with engine.begin() as conn:
        email_col = next(
            (r for r in conn.execute(text("PRAGMA table_info(users)")).fetchall() if r[1] == "email"), None
        )
        if email_col is not None and email_col[3]:  # notnull
            _rebuild_users_email_nullable(conn)
        if "phone" not in {r[1] for r in conn.execute(text("PRAGMA table_info(users)")).fetchall()}:
            conn.execute(text("ALTER TABLE users ADD COLUMN phone VARCHAR(20)"))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_phone ON users (phone)"))
        rows = conn.execute(text("PRAGMA table_info(users)")).fetchall()
        cols = {r[1] for r in rows}
        statements = [
            ("last_name", "ALTER TABLE users ADD COLUMN last_name VARCHAR(255)"),
            ("first_name", "ALTER TABLE users ADD COLUMN first_name VARCHAR(255)"),
            ("patronymic", "ALTER TABLE users ADD COLUMN patronymic VARCHAR(255)"),
            ("iin", "ALTER TABLE users ADD COLUMN iin VARCHAR(12)"),
            ("school_class", "ALTER TABLE users ADD COLUMN school_class VARCHAR(100)"),
            ("school_name", "ALTER TABLE users ADD COLUMN school_name VARCHAR(500)"),
            ("ent_profile", "ALTER TABLE users ADD COLUMN ent_profile VARCHAR(32)"),
            ("signup_approved", "ALTER TABLE users ADD COLUMN signup_approved BOOLEAN DEFAULT 1"),
            ("email_verified", "ALTER TABLE users ADD COLUMN email_verified BOOLEAN DEFAULT 1"),
            ("email_verify_token_hash", "ALTER TABLE users ADD COLUMN email_verify_token_hash VARCHAR(64)"),
            ("email_verify_expires_at", "ALTER TABLE users ADD COLUMN email_verify_expires_at DATETIME"),
        ]
        for col, stmt in statements:
            if col not in cols:
                conn.execute(text(stmt))
        cols_after = {r[1] for r in conn.execute(text("PRAGMA table_info(users)")).fetchall()}
        if "signup_approved" in cols_after:
            conn.execute(text("UPDATE users SET signup_approved = 1 WHERE signup_approved IS NULL"))
        if "email_verified" in cols_after:
            conn.execute(text("UPDATE users SET email_verified = 1 WHERE email_verified IS NULL"))

    with Session(engine) as db:
        for u in db.query(User).all():
            dirty = False
            if not u.last_name and not u.first_name:
                if u.name:
                    parts = str(u.name).split()
                    if len(parts) == 1:
                        u.last_name = "-"
                        u.first_name = parts[0][:255]
                    else:
                        u.last_name = parts[0][:255]
                        u.first_name = " ".join(parts[1:])[:255]
                else:
                    u.first_name = "User"
                    u.last_name = "-"
                dirty = True
            if not u.iin or not str(u.iin).strip():
                while True:
                    cand = "".join(str(secrets.randbelow(10)) for _ in range(12))
                    if db.query(User).filter(User.iin == cand).first() is None:
                        u.iin = cand
                        break
                dirty = True
            if not u.school_class:
                u.school_class = "-"
                dirty = True
            if not u.school_name:
                u.school_name = "-"
                dirty = True
            if not u.ent_profile:
                u.ent_profile = "FIZMAT"
                dirty = True
            if u.signup_approved is None:
                u.signup_approved = True
                dirty = True
            if u.email_verified is None:
                u.email_verified = True
                dirty = True
            if u.name is None and u.last_name and u.first_name:
                u.name = " ".join(
                    p for p in [u.last_name, u.first_name, u.patronymic or ""] if p
                ).strip()[:500]
                dirty = True
            if dirty:
                db.add(u)
        db.commit()


def migrate_questions(engine) -> None:
    """SQLite: add question helper columns if missing. Other DBs: rely on schema deploy / create on fresh DB."""
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(questions)")).fetchall()
        cols = {r[1] for r in rows}
        statements = [
            ("shuffle_answer_options", "ALTER TABLE questions ADD COLUMN shuffle_answer_options BOOLEAN DEFAULT 0"),
            ("show_calculator", "ALTER TABLE questions ADD COLUMN show_calculator BOOLEAN DEFAULT 0"),
            ("show_periodic_table", "ALTER TABLE questions ADD COLUMN show_periodic_table BOOLEAN DEFAULT 0"),
            ("language", "ALTER TABLE questions ADD COLUMN language VARCHAR(16) DEFAULT 'russian'"),
            ("difficulty", "ALTER TABLE questions ADD COLUMN difficulty VARCHAR(1) DEFAULT 'B'"),
        ]
        for col, stmt in statements:
            if col not in cols:
                conn.execute(text(stmt))
        cols_after = {r[1] for r in conn.execute(text("PRAGMA table_info(questions)")).fetchall()}
        if "language" in cols_after:
            conn.execute(text("UPDATE questions SET language = 'russian' WHERE language IS NULL OR language = ''"))
        if "difficulty" in cols_after:
            conn.execute(text("UPDATE questions SET difficulty = 'B' WHERE difficulty IS NULL OR difficulty = ''"))


def migrate_tests(engine) -> None:
    """SQLite: add test-level tool columns if missing."""
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(tests)")).fetchall()
        cols = {r[1] for r in rows}
        statements = [
            ("shuffle_answer_options", "ALTER TABLE tests ADD COLUMN shuffle_answer_options BOOLEAN DEFAULT 0"),
            ("show_calculator", "ALTER TABLE tests ADD COLUMN show_calculator BOOLEAN DEFAULT 0"),
            ("show_periodic_table", "ALTER TABLE tests ADD COLUMN show_periodic_table BOOLEAN DEFAULT 0"),
            ("proctoring_version", "ALTER TABLE tests ADD COLUMN proctoring_version INTEGER DEFAULT 0"),
        ]
        for col, stmt in statements:
            if col not in cols:
                conn.execute(text(stmt))


def migrate_test_sections(engine) -> None:
    """SQLite: test_sections table and test_questions.section_id."""
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        if "test_sections" not in tables:
            conn.execute(
                text(
                    """
                    CREATE TABLE test_sections (
                        id VARCHAR(36) PRIMARY KEY,
                        test_id VARCHAR(36) NOT NULL,
                        name VARCHAR(500) NOT NULL,
                        duration_minutes INTEGER NOT NULL DEFAULT 30,
                        section_order INTEGER NOT NULL DEFAULT 0,
                        subject_picks JSON NOT NULL DEFAULT '[]',
                        FOREIGN KEY(test_id) REFERENCES tests(id) ON DELETE CASCADE
                    )
                    """
                )
            )
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_test_sections_test_id ON test_sections (test_id)")
            )
        tq_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(test_questions)")).fetchall()}
        if "section_id" not in tq_cols:
            conn.execute(
                text(
                    "ALTER TABLE test_questions ADD COLUMN section_id VARCHAR(36) "
                    "REFERENCES test_sections(id) ON DELETE SET NULL"
                )
            )


def migrate_question_reports(engine) -> None:
    """Ensure question_reports table exists (SQLite dev DBs created before the model)."""
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        if "question_reports" in tables:
            return
        conn.execute(
            text(
                """
                CREATE TABLE question_reports (
                    id VARCHAR(36) PRIMARY KEY,
                    submission_id VARCHAR(36) NOT NULL,
                    question_id VARCHAR(36) NOT NULL,
                    reporter_id VARCHAR(36) NOT NULL,
                    reason VARCHAR(64) NOT NULL,
                    comment TEXT,
                    created_at DATETIME NOT NULL,
                    FOREIGN KEY(submission_id) REFERENCES submissions(id) ON DELETE CASCADE,
                    FOREIGN KEY(question_id) REFERENCES questions(id) ON DELETE CASCADE,
                    FOREIGN KEY(reporter_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_question_report_per_submission "
                "ON question_reports (submission_id, question_id)"
            )
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_question_reports_submission_id ON question_reports (submission_id)")
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_question_reports_question_id ON question_reports (question_id)")
        )


def migrate_proctoring(engine) -> None:
    """SQLite tables for olymp-app-style proctoring artifacts."""
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        if "proctoring_reports" not in tables:
            conn.execute(
                text(
                    """
                    CREATE TABLE proctoring_reports (
                        id VARCHAR(36) PRIMARY KEY,
                        submission_id VARCHAR(36) NOT NULL,
                        student_id VARCHAR(36) NOT NULL,
                        test_id VARCHAR(36) NOT NULL,
                        type VARCHAR(64) NOT NULL,
                        timecode VARCHAR(32) NOT NULL DEFAULT '',
                        filename VARCHAR(255) NOT NULL DEFAULT '',
                        description TEXT,
                        created_at DATETIME NOT NULL,
                        FOREIGN KEY(submission_id) REFERENCES submissions(id) ON DELETE CASCADE,
                        FOREIGN KEY(student_id) REFERENCES users(id) ON DELETE CASCADE,
                        FOREIGN KEY(test_id) REFERENCES tests(id) ON DELETE CASCADE
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_proctoring_reports_submission_id "
                    "ON proctoring_reports (submission_id)"
                )
            )
        if "proctoring_detection_logs" not in tables:
            conn.execute(
                text(
                    """
                    CREATE TABLE proctoring_detection_logs (
                        id VARCHAR(36) PRIMARY KEY,
                        submission_id VARCHAR(36) NOT NULL,
                        student_id VARCHAR(36) NOT NULL,
                        test_id VARCHAR(36) NOT NULL,
                        filename VARCHAR(255) NOT NULL DEFAULT '',
                        start_timecode VARCHAR(32) NOT NULL DEFAULT '',
                        logs TEXT NOT NULL DEFAULT '',
                        created_at DATETIME NOT NULL,
                        FOREIGN KEY(submission_id) REFERENCES submissions(id) ON DELETE CASCADE,
                        FOREIGN KEY(student_id) REFERENCES users(id) ON DELETE CASCADE,
                        FOREIGN KEY(test_id) REFERENCES tests(id) ON DELETE CASCADE
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_proctoring_detection_logs_submission_id "
                    "ON proctoring_detection_logs (submission_id)"
                )
            )
        if "proctoring_chunks" not in tables:
            conn.execute(
                text(
                    """
                    CREATE TABLE proctoring_chunks (
                        id VARCHAR(36) PRIMARY KEY,
                        submission_id VARCHAR(36) NOT NULL,
                        student_id VARCHAR(36) NOT NULL,
                        test_id VARCHAR(36) NOT NULL,
                        source VARCHAR(32) NOT NULL,
                        file_name VARCHAR(255) NOT NULL,
                        url VARCHAR(500) NOT NULL,
                        size_bytes INTEGER NOT NULL DEFAULT 0,
                        is_final BOOLEAN NOT NULL DEFAULT 0,
                        created_at DATETIME NOT NULL,
                        FOREIGN KEY(submission_id) REFERENCES submissions(id) ON DELETE CASCADE,
                        FOREIGN KEY(student_id) REFERENCES users(id) ON DELETE CASCADE,
                        FOREIGN KEY(test_id) REFERENCES tests(id) ON DELETE CASCADE
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_proctoring_chunks_submission_id "
                    "ON proctoring_chunks (submission_id)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_proctoring_chunks_file_name "
                    "ON proctoring_chunks (file_name)"
                )
            )
