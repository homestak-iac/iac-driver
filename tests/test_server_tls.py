"""Tests for server/tls.py - TLS certificate management."""

import os
import subprocess
import tempfile
from unittest.mock import patch, MagicMock

import pytest

from server.tls import (
    TLSConfig,
    generate_self_signed_cert,
    get_cert_fingerprint,
    get_hostname,
    get_primary_ip,
    verify_cert_key_match,
    DEFAULT_CERT_DAYS,
    DEFAULT_KEY_SIZE,
)


class TestTLSConfig:
    """Tests for TLSConfig dataclass."""

    def test_from_paths_success(self, tmp_path):
        """TLSConfig.from_paths creates config from existing files."""
        # Generate a real cert for testing
        cert_path = tmp_path / "test.crt"
        key_path = tmp_path / "test.key"

        subprocess.run(
            [
                "openssl", "req",
                "-x509", "-nodes",
                "-newkey", "rsa:2048",
                "-keyout", str(key_path),
                "-out", str(cert_path),
                "-days", "1",
                "-subj", "/CN=test",
            ],
            check=True,
            capture_output=True,
        )

        config = TLSConfig.from_paths(cert_path, key_path)

        assert config.cert_path == cert_path
        assert config.key_path == key_path
        assert len(config.fingerprint) > 0
        assert ":" in config.fingerprint  # Fingerprint has colons

    def test_from_paths_cert_not_found(self, tmp_path):
        """TLSConfig.from_paths raises FileNotFoundError for missing cert."""
        key_path = tmp_path / "test.key"
        key_path.touch()

        with pytest.raises(FileNotFoundError) as exc_info:
            TLSConfig.from_paths(tmp_path / "nonexistent.crt", key_path)
        assert "Certificate not found" in str(exc_info.value)

    def test_from_paths_key_not_found(self, tmp_path):
        """TLSConfig.from_paths raises FileNotFoundError for missing key."""
        cert_path = tmp_path / "test.crt"
        cert_path.touch()

        with pytest.raises(FileNotFoundError) as exc_info:
            TLSConfig.from_paths(cert_path, tmp_path / "nonexistent.key")
        assert "Key not found" in str(exc_info.value)


class TestGetCertFingerprint:
    """Tests for get_cert_fingerprint function."""

    def test_fingerprint_format(self, tmp_path):
        """Fingerprint has correct format (hex with colons)."""
        # Generate a cert
        cert_path = tmp_path / "test.crt"
        key_path = tmp_path / "test.key"

        subprocess.run(
            [
                "openssl", "req",
                "-x509", "-nodes",
                "-newkey", "rsa:2048",
                "-keyout", str(key_path),
                "-out", str(cert_path),
                "-days", "1",
                "-subj", "/CN=test",
            ],
            check=True,
            capture_output=True,
        )

        fingerprint = get_cert_fingerprint(cert_path)

        # SHA256 fingerprint should have 64 hex chars + 31 colons
        assert len(fingerprint) == 95
        assert fingerprint.count(":") == 31
        # All parts should be hex
        for part in fingerprint.split(":"):
            int(part, 16)  # Should not raise ValueError

    def test_fingerprint_invalid_cert(self, tmp_path):
        """get_cert_fingerprint raises on invalid certificate."""
        cert_path = tmp_path / "invalid.crt"
        cert_path.write_text("not a certificate")

        with pytest.raises(subprocess.CalledProcessError):
            get_cert_fingerprint(cert_path)


class TestHelperFunctions:
    """Tests for helper functions."""

    def test_get_hostname(self):
        """get_hostname returns a non-empty string."""
        hostname = get_hostname()
        assert isinstance(hostname, str)
        assert len(hostname) > 0

    def test_get_primary_ip_returns_ip_or_none(self):
        """get_primary_ip returns an IP address or None."""
        ip = get_primary_ip()
        if ip is not None:
            # Validate it looks like an IP
            parts = ip.split(".")
            assert len(parts) == 4
            for part in parts:
                assert 0 <= int(part) <= 255

    def test_get_primary_ip_handles_socket_error(self):
        """get_primary_ip returns None on socket error."""
        with patch("socket.socket") as mock_socket:
            mock_socket.return_value.connect.side_effect = OSError("Network error")
            ip = get_primary_ip()
            assert ip is None


class TestGenerateSelfSignedCert:
    """Tests for generate_self_signed_cert function."""

    def test_generates_new_cert(self, tmp_path):
        """generate_self_signed_cert creates cert and key files."""
        config = generate_self_signed_cert(
            cert_dir=tmp_path,
            hostname="test-host",
            days=1,
            key_size=2048,  # Smaller for faster tests
        )

        assert config.cert_path.exists()
        assert config.key_path.exists()
        assert len(config.fingerprint) > 0

        # Check file permissions
        key_stat = config.key_path.stat()
        assert key_stat.st_mode & 0o777 == 0o600

        cert_stat = config.cert_path.stat()
        assert cert_stat.st_mode & 0o777 == 0o644

    def test_uses_existing_cert_without_force(self, tmp_path):
        """generate_self_signed_cert reuses existing cert when force=False."""
        # Generate first cert
        config1 = generate_self_signed_cert(
            cert_dir=tmp_path, hostname="test", key_size=2048
        )
        fingerprint1 = config1.fingerprint

        # Try to generate again without force
        config2 = generate_self_signed_cert(
            cert_dir=tmp_path, hostname="test", key_size=2048
        )
        fingerprint2 = config2.fingerprint

        # Should be same cert
        assert fingerprint1 == fingerprint2

    def test_overwrites_with_force(self, tmp_path):
        """generate_self_signed_cert overwrites existing cert when force=True."""
        # Generate first cert
        config1 = generate_self_signed_cert(
            cert_dir=tmp_path, hostname="test", key_size=2048
        )
        fingerprint1 = config1.fingerprint

        # Generate again with force
        config2 = generate_self_signed_cert(
            cert_dir=tmp_path, hostname="test", key_size=2048, force=True
        )
        fingerprint2 = config2.fingerprint

        # Should be different cert (extremely unlikely to match)
        assert fingerprint1 != fingerprint2

    def test_creates_directory(self, tmp_path):
        """generate_self_signed_cert creates cert_dir if it doesn't exist."""
        nested_dir = tmp_path / "nested" / "cert" / "dir"
        assert not nested_dir.exists()

        config = generate_self_signed_cert(
            cert_dir=nested_dir, hostname="test", key_size=2048
        )

        assert nested_dir.exists()
        assert config.cert_path.exists()

    def test_includes_san_with_hostname(self, tmp_path):
        """Certificate includes hostname in SAN."""
        config = generate_self_signed_cert(
            cert_dir=tmp_path, hostname="my-controller", key_size=2048
        )

        # Extract SAN from certificate
        result = subprocess.run(
            [
                "openssl", "x509",
                "-in", str(config.cert_path),
                "-noout",
                "-text",
            ],
            capture_output=True,
            text=True,
            check=True,
        )

        assert "DNS:my-controller" in result.stdout

    def test_includes_san_with_ip(self, tmp_path):
        """Certificate includes IP address in SAN when available."""
        with patch("server.tls.get_primary_ip", return_value="198.51.100.10"):
            config = generate_self_signed_cert(
                cert_dir=tmp_path, hostname="my-controller", key_size=2048
            )

            # Extract SAN from certificate
            result = subprocess.run(
                [
                    "openssl", "x509",
                    "-in", str(config.cert_path),
                    "-noout",
                    "-text",
                ],
                capture_output=True,
                text=True,
                check=True,
            )

            assert "IP Address:198.51.100.10" in result.stdout


class TestVerifyCertKeyMatch:
    """Tests for verify_cert_key_match function."""

    def test_matching_cert_and_key(self, tmp_path):
        """verify_cert_key_match returns True for matching pair."""
        cert_path = tmp_path / "test.crt"
        key_path = tmp_path / "test.key"

        subprocess.run(
            [
                "openssl", "req",
                "-x509", "-nodes",
                "-newkey", "rsa:2048",
                "-keyout", str(key_path),
                "-out", str(cert_path),
                "-days", "1",
                "-subj", "/CN=test",
            ],
            check=True,
            capture_output=True,
        )

        assert verify_cert_key_match(cert_path, key_path) is True

    def test_mismatched_cert_and_key(self, tmp_path):
        """verify_cert_key_match returns False for mismatched pair."""
        # Generate two different key pairs
        cert1 = tmp_path / "cert1.crt"
        key1 = tmp_path / "key1.key"
        cert2 = tmp_path / "cert2.crt"
        key2 = tmp_path / "key2.key"

        for cert, key in [(cert1, key1), (cert2, key2)]:
            subprocess.run(
                [
                    "openssl", "req",
                    "-x509", "-nodes",
                    "-newkey", "rsa:2048",
                    "-keyout", str(key),
                    "-out", str(cert),
                    "-days", "1",
                    "-subj", "/CN=test",
                ],
                check=True,
                capture_output=True,
            )

        # Mix cert from pair 1 with key from pair 2
        assert verify_cert_key_match(cert1, key2) is False

    def test_invalid_cert_returns_false(self, tmp_path):
        """verify_cert_key_match returns False for invalid cert."""
        cert_path = tmp_path / "invalid.crt"
        key_path = tmp_path / "test.key"

        # Create valid key
        subprocess.run(
            [
                "openssl", "genrsa",
                "-out", str(key_path),
                "2048",
            ],
            check=True,
            capture_output=True,
        )

        # Create invalid cert
        cert_path.write_text("not a certificate")

        assert verify_cert_key_match(cert_path, key_path) is False
