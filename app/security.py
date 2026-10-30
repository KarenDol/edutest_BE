from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 7
ACCESS_TOKEN_REMEMBER_DAYS = 30


def _bcrypt_password(plain: str) -> str:
    """Bcrypt only uses the first 72 UTF-8 bytes; normalize so hash and verify match."""
    raw = plain.encode("utf-8")[:72]
    return raw.decode("utf-8", errors="ignore")


def hash_password(password: str) -> str:
    return pwd_context.hash(_bcrypt_password(password))


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(_bcrypt_password(plain), hashed)


def create_access_token(
    user_id: str,
    email: str | None,
    role: str,
    name: str | None,
    *,
    expire_days: int | None = None,
) -> str:
    days = expire_days if expire_days is not None else ACCESS_TOKEN_EXPIRE_DAYS
    expire = datetime.now(timezone.utc) + timedelta(days=days)
    payload = {
        "sub": user_id,
        "email": email or "",
        "role": role,
        "name": name or "",
        "exp": expire,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except JWTError:
        return None
