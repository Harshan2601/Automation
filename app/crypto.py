from cryptography.fernet import Fernet, InvalidToken
from app.config import TOKEN_ENCRYPTION_KEY


def _fernet() -> Fernet:
    if not TOKEN_ENCRYPTION_KEY:
        raise RuntimeError(
            "TOKEN_ENCRYPTION_KEY is not set. Generate one with: "
            "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    try:
        return Fernet(TOKEN_ENCRYPTION_KEY.encode("utf-8"))
    except Exception as exc:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not a valid Fernet key") from exc


def encrypt_secret(value: str | None) -> str | None:
    if not value:
        return None
    return _fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError(
            "Cannot decrypt a stored token. Check TOKEN_ENCRYPTION_KEY; do not generate a new key "
            "while encrypted tokens still need to be read."
        ) from exc
