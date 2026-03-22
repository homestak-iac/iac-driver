"""Config endpoint handler for the server.

Serves scoped site config + secrets for PVE nodes via /config/{identity}.
Authenticated by provisioning token (same as /spec). Distinct purpose:
/spec serves what a VM should become; /config serves operational config
for PVE nodes (site defaults, secrets, private key).

See docs/pve-self-configure.md for design rationale.
"""

import logging
from pathlib import Path
from typing import Tuple

from server.auth import extract_bearer_token, verify_provisioning_token, AuthError

logger = logging.getLogger(__name__)


def handle_config_request(
    identity: str,
    auth_header: str,
    signing_key: str,
    site_config: dict,
    secrets: dict,
) -> Tuple[dict, int]:
    """Handle a /config/{identity} request with provisioning token auth.

    Args:
        identity: Node identity from URL path
        auth_header: Authorization header from request
        signing_key: Hex-encoded signing key for token verification
        site_config: Loaded site.yaml (defaults section)
        secrets: Loaded secrets.yaml

    Returns:
        Tuple of (response_dict, http_status)
    """
    # Extract and verify provisioning token
    token = extract_bearer_token(auth_header)
    if not token:
        return _error_response("E300", "Provisioning token required"), 400

    if not signing_key:
        return _error_response("E500", "Signing key not configured"), 500

    try:
        claims = verify_provisioning_token(token, signing_key, identity)
    except AuthError as e:
        logger.warning(
            "Config token verification failed: %s %s node=%s",
            e.code, e.message, identity,
        )
        return _error_response(e.code, e.message), e.http_status

    logger.info("Config token verified: n=%s iat=%s", claims["n"], claims.get("iat"))

    # Build scoped response
    try:
        response = _build_config_response(site_config, secrets)
        return response, 200
    except Exception as e:
        logger.exception("Error building config response for %s", identity)
        return _error_response("E500", f"Internal error: {e}"), 500


def _build_config_response(site_config: dict, secrets: dict) -> dict:
    """Build scoped config response.

    Scoping rules:
    - site.*: All defaults included (DNS, gateway, timezone, etc.)
    - secrets.signing_key: Yes (needed to mint tokens for child VMs)
    - secrets.ssh_keys: Yes (injected into child VMs)
    - secrets.passwords: Yes (needed for child VMs)
    - secrets.api_tokens: EXCLUDED (each PVE node generates its own)
    - secrets.private_key: Included for dev posture (shared key model)
    """
    # Scope secrets — exclude api_tokens
    scoped_secrets = {}
    for key, value in secrets.items():
        if key == "api_tokens":
            continue
        scoped_secrets[key] = value

    # Include private key (dev posture — shared key model)
    # In prod posture, each node generates its own keypair
    key_path = Path.home() / ".ssh" / "id_rsa"
    if key_path.exists():
        try:
            scoped_secrets["private_key"] = key_path.read_text(encoding="utf-8").strip()
        except OSError as e:
            logger.warning("Could not read private key %s: %s", key_path, e)

    return {
        "site": site_config.get("defaults", {}),
        "secrets": scoped_secrets,
    }


def _error_response(code: str, message: str) -> dict:
    """Build error response dict."""
    return {"error": {"code": code, "message": message}}
