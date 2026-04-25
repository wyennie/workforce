"""GitHub webhook signature verification.

GitHub signs every webhook payload with HMAC-SHA256 using the shared secret
configured in the repo's webhook settings. The signature arrives in the
``X-Hub-Signature-256`` header as ``sha256=<hex>``.
"""

from __future__ import annotations

import hashlib
import hmac


def verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify a GitHub webhook HMAC-SHA256 signature.

    Args:
        payload: The raw request body bytes.
        signature: The value of the ``X-Hub-Signature-256`` header,
            expected in the form ``sha256=<hex>``.
        secret: The shared webhook secret configured in GitHub.

    Returns:
        True if the signature is valid, False otherwise.
    """
    if not signature.startswith("sha256="):
        return False
    expected = hmac.new(
        secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)
