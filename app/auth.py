import hashlib
import secrets
from .models import Staff

# Stdlib-only password/PIN hashing (PBKDF2-HMAC-SHA256). No external crypto deps,
# so deployment can't break on a bcrypt version mismatch.
_ITERATIONS = 120_000


def hash_pin(pin: str, salt: str | None = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), _ITERATIONS)
    return dk.hex(), salt


def verify_pin(pin: str, pin_hash: str, salt: str) -> bool:
    calc, _ = hash_pin(pin, salt)
    return secrets.compare_digest(calc, pin_hash)


def current_staff(request, db) -> Staff | None:
    sid = request.session.get("staff_id")
    if not sid:
        return None
    staff = db.get(Staff, sid)
    if staff and staff.is_active:
        return staff
    return None
