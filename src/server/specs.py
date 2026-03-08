"""Spec endpoint handler for the server.

Serves resolved specs from config/specs/ with provisioning token auth (#231).
"""

import logging
from typing import Tuple

from resolver.spec_resolver import (
    SpecResolver,
    SpecNotFoundError,
    SchemaValidationError,
)
from resolver.base import (
    ResolverError,
    PostureNotFoundError,
    SSHKeyNotFoundError,
)
from server.auth import extract_bearer_token, verify_provisioning_token, AuthError

logger = logging.getLogger(__name__)


def handle_spec_request(
    identity: str,
    auth_header: str,
    resolver: SpecResolver,
    signing_key: str,
) -> Tuple[dict, int]:
    """Handle a spec request with provisioning token authentication.

    Requires a valid provisioning token. The spec is resolved using the
    token's 's' claim (spec FK), not the URL identity.

    Args:
        identity: Node identity from URL path (e.g., "edge")
        auth_header: Authorization header from request
        resolver: SpecResolver instance
        signing_key: Hex-encoded signing key for token verification

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
            "Token verification failed: %s %s node=%s",
            e.code, e.message, identity,
        )
        return _error_response(e.code, e.message), e.http_status

    # Extract spec FK from token claims
    spec_name = claims["s"]
    logger.info("Token verified: n=%s s=%s iat=%s", claims["n"], spec_name, claims.get("iat"))

    # Resolve spec using the token's spec FK
    try:
        spec = resolver.resolve(spec_name)

        # Remove internal _posture field from response
        if "access" in spec and "_posture" in spec["access"]:
            spec = dict(spec)
            spec["access"] = {k: v for k, v in spec["access"].items() if k != "_posture"}

        return spec, 200

    except SpecNotFoundError:
        return _error_response("E200", f"Spec not found: {spec_name}"), 404
    except PostureNotFoundError as e:
        return _error_response(e.code, e.message), 404
    except SSHKeyNotFoundError as e:
        return _error_response(e.code, e.message), 404
    except SchemaValidationError as e:
        return _error_response(e.code, e.message), 422
    except ResolverError as e:
        return _error_response(e.code, e.message), 500
    except Exception as e:
        logger.exception("Unexpected error resolving spec %s", spec_name)
        return _error_response("E500", f"Internal error: {e}"), 500


def handle_specs_list(resolver: SpecResolver) -> Tuple[dict, int]:
    """Handle a request to list available specs.

    Args:
        resolver: SpecResolver instance

    Returns:
        Tuple of (response_dict, http_status)
    """
    try:
        specs = resolver.list_specs()
        return {"specs": specs}, 200
    except ResolverError as e:
        return _error_response(e.code, e.message), 500
    except Exception as e:
        logger.exception("Unexpected error listing specs")
        return _error_response("E500", f"Internal error: {e}"), 500


def _error_response(code: str, message: str) -> dict:
    """Build error response dict."""
    return {"error": {"code": code, "message": message}}
