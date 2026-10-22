import hashlib
import hmac

from fastapi import HTTPException


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify(body: bytes, signature: str | None, secret: str) -> None:
    if signature is None or not hmac.compare_digest(sign(body, secret), signature):
        raise HTTPException(status_code=401, detail="Invalid X-Signature")
