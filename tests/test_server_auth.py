"""Tests for server/auth.py - authentication middleware."""

import base64
import hashlib
import hmac
import json
import os
import time
from unittest.mock import MagicMock, patch

import pytest

from conftest import TEST_SIGNING_KEY, mint_test_token
from server.auth import (
    AuthError,
    extract_bearer_token,
    verify_provisioning_token,
    validate_repo_token,
    _base64url_decode,
)


class TestAuthError:
    """Tests for AuthError dataclass."""

    def test_auth_error_fields(self):
        """AuthError has correct fields."""
        error = AuthError(code="E300", message="Auth required", http_status=401)
        assert error.code == "E300"
        assert error.message == "Auth required"
        assert error.http_status == 401


class TestExtractBearerToken:
    """Tests for extract_bearer_token function."""

    def test_valid_bearer_token(self):
        """Extracts token from valid Bearer header."""
        token = extract_bearer_token("Bearer my-secret-token")
        assert token == "my-secret-token"

    def test_empty_header(self):
        """Returns None for empty header."""
        token = extract_bearer_token("")
        assert token is None

    def test_none_header(self):
        """Returns None for None header."""
        token = extract_bearer_token(None)
        assert token is None

    def test_basic_auth_header(self):
        """Returns None for Basic auth header."""
        token = extract_bearer_token("Basic dXNlcjpwYXNz")
        assert token is None

    def test_bearer_case_sensitive(self):
        """Bearer prefix is case-sensitive."""
        token = extract_bearer_token("bearer my-token")
        assert token is None

    def test_bearer_with_no_token(self):
        """Returns empty string for Bearer with no token."""
        token = extract_bearer_token("Bearer ")
        assert token == ""


class TestBase64UrlDecode:
    """Tests for _base64url_decode helper."""

    def test_decode_no_padding(self):
        """Decodes base64url without padding."""
        encoded = base64.urlsafe_b64encode(b"hello").rstrip(b'=').decode()
        assert _base64url_decode(encoded) == b"hello"

    def test_decode_with_padding(self):
        """Decodes base64url that already has padding."""
        encoded = base64.urlsafe_b64encode(b"test").decode()
        assert _base64url_decode(encoded) == b"test"

    def test_decode_url_safe_chars(self):
        """Handles URL-safe characters (- and _ instead of + and /)."""
        # Data that would produce + or / in standard base64
        data = bytes(range(256))
        encoded = base64.urlsafe_b64encode(data).rstrip(b'=').decode()
        assert _base64url_decode(encoded) == data


class TestVerifyProvisioningToken:
    """Tests for verify_provisioning_token function."""

    def test_valid_token(self):
        """Valid token returns decoded claims."""
        token = mint_test_token("edge", "base")
        claims = verify_provisioning_token(token, TEST_SIGNING_KEY, "edge")

        assert claims["v"] == 1
        assert claims["n"] == "edge"
        assert claims["s"] == "base"
        assert "iat" in claims

    def test_valid_token_different_spec(self):
        """Token with different spec FK is valid."""
        token = mint_test_token("pve-node", "pve")
        claims = verify_provisioning_token(token, TEST_SIGNING_KEY, "pve-node")

        assert claims["s"] == "pve"

    def test_malformed_token_no_dots(self):
        """Rejects token without dot separator."""
        with pytest.raises(AuthError) as exc_info:
            verify_provisioning_token("nodots", TEST_SIGNING_KEY, "edge")
        assert exc_info.value.code == "E300"
        assert exc_info.value.http_status == 400

    def test_malformed_token_too_many_dots(self):
        """Rejects token with too many dot segments."""
        with pytest.raises(AuthError) as exc_info:
            verify_provisioning_token("a.b.c", TEST_SIGNING_KEY, "edge")
        assert exc_info.value.code == "E300"
        assert exc_info.value.http_status == 400

    def test_invalid_signature(self):
        """Rejects token with wrong signature."""
        token = mint_test_token("edge", "base")
        # Use a different signing key to verify
        wrong_key = "b" * 64
        with pytest.raises(AuthError) as exc_info:
            verify_provisioning_token(token, wrong_key, "edge")
        assert exc_info.value.code == "E301"
        assert exc_info.value.http_status == 401

    def test_tampered_payload(self):
        """Rejects token with modified payload."""
        token = mint_test_token("edge", "base")
        payload_b64, sig_b64 = token.split(".")

        # Tamper with payload (change a character)
        tampered = list(payload_b64)
        tampered[5] = 'X' if tampered[5] != 'X' else 'Y'
        tampered_token = f"{''.join(tampered)}.{sig_b64}"

        with pytest.raises(AuthError) as exc_info:
            verify_provisioning_token(tampered_token, TEST_SIGNING_KEY, "edge")
        # Could be E300 (bad payload) or E301 (bad sig) depending on what X decodes to
        assert exc_info.value.http_status in (400, 401)

    def test_identity_mismatch(self):
        """Rejects token when URL identity doesn't match token identity."""
        token = mint_test_token("edge", "base")
        with pytest.raises(AuthError) as exc_info:
            verify_provisioning_token(token, TEST_SIGNING_KEY, "wrong-identity")
        assert exc_info.value.code == "E301"
        assert "mismatch" in exc_info.value.message

    def test_unsupported_version(self):
        """Rejects token with unsupported version."""
        token = mint_test_token("edge", "base", v=2)
        with pytest.raises(AuthError) as exc_info:
            verify_provisioning_token(token, TEST_SIGNING_KEY, "edge")
        assert exc_info.value.code == "E300"
        assert "version" in exc_info.value.message.lower()

    def test_missing_n_claim(self):
        """Rejects token without 'n' claim."""
        # Build token manually without 'n'
        payload = {"v": 1, "s": "base", "iat": int(time.time())}
        payload_bytes = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(',', ':')).encode()
        ).rstrip(b'=')
        signature = hmac.new(
            bytes.fromhex(TEST_SIGNING_KEY), payload_bytes, hashlib.sha256
        ).digest()
        sig_bytes = base64.urlsafe_b64encode(signature).rstrip(b'=')
        token = f"{payload_bytes.decode()}.{sig_bytes.decode()}"

        with pytest.raises(AuthError) as exc_info:
            verify_provisioning_token(token, TEST_SIGNING_KEY, "edge")
        assert exc_info.value.code == "E300"
        assert "missing required claims" in exc_info.value.message

    def test_missing_s_claim(self):
        """Rejects token without 's' claim."""
        payload = {"v": 1, "n": "edge", "iat": int(time.time())}
        payload_bytes = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(',', ':')).encode()
        ).rstrip(b'=')
        signature = hmac.new(
            bytes.fromhex(TEST_SIGNING_KEY), payload_bytes, hashlib.sha256
        ).digest()
        sig_bytes = base64.urlsafe_b64encode(signature).rstrip(b'=')
        token = f"{payload_bytes.decode()}.{sig_bytes.decode()}"

        with pytest.raises(AuthError) as exc_info:
            verify_provisioning_token(token, TEST_SIGNING_KEY, "edge")
        assert exc_info.value.code == "E300"
        assert "missing required claims" in exc_info.value.message

    def test_invalid_signing_key_hex(self):
        """Rejects operation with malformed signing key."""
        token = mint_test_token("edge", "base")
        with pytest.raises(AuthError) as exc_info:
            verify_provisioning_token(token, "not-hex", "edge")
        assert exc_info.value.code == "E500"
        assert exc_info.value.http_status == 500

    def test_invalid_sig_encoding(self):
        """Rejects token with non-base64 signature."""
        payload = {"v": 1, "n": "edge", "s": "base", "iat": int(time.time())}
        payload_bytes = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(',', ':')).encode()
        ).rstrip(b'=')
        token = f"{payload_bytes.decode()}.!!!invalid!!!"

        with pytest.raises(AuthError) as exc_info:
            verify_provisioning_token(token, TEST_SIGNING_KEY, "edge")
        assert exc_info.value.http_status in (400, 401)

    def test_iat_claim_preserved(self):
        """iat (issued-at) claim is preserved in returned claims."""
        now = int(time.time())
        token = mint_test_token("edge", "base", iat=now)
        claims = verify_provisioning_token(token, TEST_SIGNING_KEY, "edge")
        assert claims["iat"] == now


class TestValidateRepoToken:
    """Tests for validate_repo_token function."""

    def test_valid_token(self):
        """Validates correct repo token."""
        error = validate_repo_token("Bearer correct-token", "correct-token")
        assert error is None

    def test_missing_token(self):
        """Fails when no token provided."""
        error = validate_repo_token("", "expected-token")
        assert error is not None
        assert error.code == "E300"
        assert error.http_status == 401

    def test_wrong_token(self):
        """Fails with incorrect token."""
        error = validate_repo_token("Bearer wrong-token", "correct-token")
        assert error is not None
        assert error.code == "E301"
        assert error.http_status == 403

    def test_empty_expected_token_disables_auth(self):
        """Empty expected token disables auth (dev mode)."""
        error = validate_repo_token("", "")
        assert error is None

    def test_dev_mode_accepts_any_token(self):
        """Dev mode (empty expected) accepts any token."""
        error = validate_repo_token("Bearer any-token", "")
        assert error is None

    def test_basic_auth_header_fails(self):
        """Basic auth header fails (not Bearer)."""
        error = validate_repo_token("Basic dXNlcjpwYXNz", "some-token")
        assert error is not None
        assert error.code == "E300"
