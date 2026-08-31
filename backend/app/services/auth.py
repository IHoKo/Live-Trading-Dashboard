"""Single-user passphrase auth — plan.md §11.

"This is a single-user app on the public internet; assume it will be found."

Design notes that matter:

* The session cookie is a **signed, timestamped token**, not a session id in a
  table. There is one user and one machine; a server-side session store would
  add a write path and a migration for no benefit.
* `HttpOnly` keeps it out of `document.cookie`, so an XSS bug cannot read it.
  `Secure` keeps it off plaintext HTTP. `SameSite=Lax` blocks cross-site POSTs
  from carrying it, which is the CSRF protection for every mutating route here.
* Passphrase comparison is constant-time. The margin is small on a login
  endpoint that is also rate-limited, but the cost of doing it right is zero.
"""

import logging
import secrets

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

logger = logging.getLogger("ticker.auth")

COOKIE_NAME = "ticker_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
_SALT = "ticker-session-v1"


class AuthError(Exception):
    pass


class NotConfigured(AuthError):
    """No passphrase or secret set — auth cannot be enforced or granted."""


class SessionManager:
    def __init__(self, secret: str | None, passphrase: str | None) -> None:
        self._secret = secret
        self._passphrase = passphrase
        self._serializer = URLSafeTimedSerializer(secret, salt=_SALT) if secret else None

    @property
    def configured(self) -> bool:
        return bool(self._secret and self._passphrase)

    def verify_passphrase(self, candidate: str) -> bool:
        if not self.configured:
            raise NotConfigured("APP_PASSPHRASE and SESSION_SECRET must both be set.")
        return secrets.compare_digest(candidate, self._passphrase or "")

    def issue(self) -> str:
        if self._serializer is None:
            raise NotConfigured("SESSION_SECRET is not set.")
        return self._serializer.dumps({"v": 1})

    def valid(self, token: str | None) -> bool:
        if not token or self._serializer is None:
            return False
        try:
            self._serializer.loads(token, max_age=SESSION_MAX_AGE)
        except SignatureExpired:
            return False
        except BadSignature:
            logger.warning("rejected a session cookie with a bad signature")
            return False
        return True
