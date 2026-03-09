#!/usr/bin/env python3
"""Tests for readiness checks."""

from unittest.mock import patch, MagicMock

import pytest
from readiness import (
    validate_api_token,
    validate_host_resolvable,
    validate_host_reachable,
    validate_host,
)


class TestValidateApiToken:
    """Test API token validation."""

    def test_valid_token_returns_success(self):
        """Valid token returns success with version."""
        with patch('readiness.requests.get') as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {
                "data": {"version": "8.1.3"}
            }

            success, message = validate_api_token(
                "https://localhost:8006",
                "root@pam!homestak=abc123"
            )

            assert success is True
            assert "8.1.3" in message

    def test_invalid_token_returns_failure(self):
        """Invalid token returns failure with remediation."""
        with patch('readiness.requests.get') as mock_get:
            mock_get.return_value.status_code = 401

            success, message = validate_api_token(
                "https://localhost:8006",
                "root@pam!bad=token"
            )

            assert success is False
            assert "Invalid API token" in message
            assert "pveum user token add" in message

    def test_connection_error_returns_failure(self):
        """Connection error returns descriptive failure."""
        with patch('readiness.requests.get') as mock_get:
            mock_get.side_effect = Exception("Connection refused")

            success, message = validate_api_token(
                "https://badhost:8006",
                "token"
            )

            assert success is False
            assert "Error" in message or "refused" in message.lower()

    def test_timeout_returns_failure(self):
        """Timeout returns descriptive failure."""
        with patch('readiness.requests.get') as mock_get:
            import requests
            mock_get.side_effect = requests.exceptions.Timeout()

            success, message = validate_api_token(
                "https://slowhost:8006",
                "token"
            )

            assert success is False
            assert "Timeout" in message


class TestValidateHostResolvable:
    """Test hostname resolution validation."""

    def test_localhost_resolves(self):
        """localhost should resolve."""
        success, message = validate_host_resolvable("localhost")
        assert success is True
        assert "resolves to" in message

    def test_ip_address_resolves(self):
        """IP address should resolve to itself."""
        success, message = validate_host_resolvable("127.0.0.1")
        assert success is True

    def test_invalid_hostname_fails(self):
        """Non-existent hostname should fail."""
        success, message = validate_host_resolvable("nonexistent.invalid.local")
        assert success is False
        assert "Cannot resolve" in message


class TestValidateHostReachable:
    """Test host reachability validation."""

    def test_unreachable_port_fails(self):
        """Unreachable port returns failure."""
        # Use a high port that's unlikely to be in use
        success, message = validate_host_reachable("127.0.0.1", port=59999, timeout=1)
        assert success is False

    def test_reachable_check_structure(self):
        """Verify return structure."""
        # Just verify we get a tuple back
        result = validate_host_reachable("127.0.0.1", port=59999, timeout=0.5)
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], str)


class TestValidateHost:
    """Test combined host validation."""

    def test_localhost_resolution(self):
        """localhost should at least resolve."""
        # Skip SSH/API checks since they may not be available
        with patch('readiness.validate_host_reachable') as mock_reach:
            mock_reach.return_value = (True, "reachable")
            success, message = validate_host("localhost", check_ssh=True, check_api=False)
            assert success is True

    def test_invalid_host_fails_early(self):
        """Invalid hostname should fail at resolution."""
        success, message = validate_host("nonexistent.invalid.local")
        assert success is False
        assert "Cannot resolve" in message

    def test_ssh_check_included(self):
        """SSH port should be checked when requested."""
        with patch('readiness.validate_host_resolvable') as mock_resolve:
            with patch('readiness.validate_host_reachable') as mock_reach:
                with patch('readiness.socket.gethostbyname') as mock_dns:
                    mock_resolve.return_value = (True, "resolves")
                    mock_reach.return_value = (False, "port closed")
                    mock_dns.return_value = "10.0.0.1"

                    success, message = validate_host("testhost", check_ssh=True, check_api=False)

                    # Should have called reachable check for SSH port 22
                    mock_reach.assert_called()
                    # Check that port=22 was passed (either positional or keyword)
                    call_args = mock_reach.call_args
                    port = call_args.kwargs.get('port', call_args.args[1] if len(call_args.args) > 1 else None)
                    assert port == 22

    def test_api_check_included(self):
        """API port should be checked when requested."""
        with patch('readiness.validate_host_resolvable') as mock_resolve:
            with patch('readiness.validate_host_reachable') as mock_reach:
                with patch('readiness.socket.gethostbyname') as mock_dns:
                    mock_resolve.return_value = (True, "resolves")
                    mock_reach.return_value = (True, "reachable")
                    mock_dns.return_value = "10.0.0.1"

                    success, message = validate_host("testhost", check_ssh=False, check_api=True)

                    # Should have called reachable check for API port 8006
                    mock_reach.assert_called()
                    call_args = mock_reach.call_args
                    port = call_args.kwargs.get('port', call_args.args[1] if len(call_args.args) > 1 else None)
                    assert port == 8006
