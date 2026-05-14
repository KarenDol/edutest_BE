import secrets

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import User


def migrate_users(engine) -> None:
    """SQLite: add new user columns if missing; backfill legacy rows."""
    with engine.begin() as conn:
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
