"""Settings read from the environment, once, at import.

Everything here is required or has a safe default, and a missing value stops the
app at startup rather than at the first request that needs it.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).parent


def required_env(name: str) -> str:
    """Read a required environment variable, failing at startup if it's unset."""
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"The {name} environment variable must be set")
    return value


# The shared password everyone logs in with. There are no user accounts.
BOARD_PASSWORD = required_env("BOARD_PASSWORD")
# Signs the session cookie. Changing it logs every device out.
SECRET_KEY = required_env("SECRET_KEY")
# The session cookie is marked Secure, and browsers only send those back over
# HTTPS or to localhost. ALLOW_HTTP=1 turns that off for serving over plain HTTP.
ALLOW_HTTP = os.environ.get("ALLOW_HTTP") == "1"

SESSION_MAX_AGE = 365 * 24 * 60 * 60  # one year
