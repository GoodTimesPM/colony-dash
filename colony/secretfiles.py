"""Which files hold credentials.

One answer for every place that hides a file from the dashboard, keeps it out
of a build worktree, or copies it into one for a contract with `sees_secrets`.
Matching is by pattern: any `.env*` except the templates, key and certificate
files, and the usual credential stores.
"""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import PurePath

EXACT = {
    "credentials.json", "service-account.json", "secrets.json", "secrets.toml",
    "token.json", ".npmrc", ".netrc", ".pypirc", ".pgpass", ".git-credentials",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
}
PATTERNS = (".env*", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore",
            "credentials*.json", "client_secret*.json", "service-account*.json")
TEMPLATES = {".env.example", ".env.sample", ".env.template", ".env.dist"}


def is_secret(path: str | PurePath) -> bool:
    """True when the file's name marks it as holding a credential."""
    name = PurePath(str(path).replace("\\", "/")).name.lower()
    if not name or name in TEMPLATES:
        return False
    return name in EXACT or any(fnmatch(name, p) for p in PATTERNS)
