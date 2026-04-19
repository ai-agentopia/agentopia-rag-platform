"""Per-source credential reader for the Pathway runtime (ADR-001 Phase 4).

`external_s3` sources carry a Vault `credential_ref` on the knowledge_sources
row. At pipeline-graph construction time we resolve that reference into
AWS access keys, which are handed to `pw.io.s3.AwsS3Settings(...)` for
just that source's watcher. No credential value ever touches the DB or
a log line.

Contract (operator-visible):

    Vault KV-v2 path  secret/data/agentopia/sources/{name}
        data:
            access_key: <string>
            secret_key: <string>

Anything else stored at that path is ignored. The `access_key` /
`secret_key` names match bot-config-api's source_credentials service, so
the same secret payload works for both validation (on create) and runtime
(on watcher startup).

Safety:
    * Missing Vault address/token → fail fast. The runtime refuses to
      stand up a watcher with no credentials.
    * 4xx/5xx from Vault → raise — the per-source subgraph build is
      skipped by pipeline.py, surfacing as a visible error while other
      sources continue.
    * All exceptions carry operator-safe messages (no credential values).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class S3Credentials:
    access_key: str
    secret_key: str


class CredentialError(Exception):
    pass


def _vault_addr() -> str:
    return os.getenv("VAULT_ADDR", "").rstrip("/")


def _vault_token() -> str:
    return os.getenv("VAULT_TOKEN", "")


def read_s3_credentials(credential_ref: str) -> S3Credentials:
    """Resolve a `credential_ref` into AWS keys via the runtime's Vault token."""
    addr = _vault_addr()
    token = _vault_token()
    if not addr or not token:
        raise CredentialError(
            "VAULT_ADDR / VAULT_TOKEN not set in the pipeline pod — "
            "external_s3 sources require runtime Vault access",
        )
    path = credential_ref.strip().lstrip("/")
    if not path:
        raise CredentialError("credential_ref is empty")
    url = f"{addr}/v1/{path}"
    req = urllib.request.Request(url, headers={"X-Vault-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise CredentialError(
            f"Vault read failed at '{credential_ref}': {type(exc).__name__}",
        ) from exc

    data = ((body or {}).get("data") or {}).get("data") or {}
    access_key = str(data.get("access_key") or "")
    secret_key = str(data.get("secret_key") or "")
    missing = [k for k, v in (("access_key", access_key), ("secret_key", secret_key)) if not v]
    if missing:
        raise CredentialError(
            f"Vault secret at '{credential_ref}' is missing keys: {missing}",
        )
    return S3Credentials(access_key=access_key, secret_key=secret_key)
