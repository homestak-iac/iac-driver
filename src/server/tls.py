"""TLS certificate management for the server.

Provides self-signed certificate auto-generation with fingerprint output
for TOFU (trust-on-first-use) verification.
"""

import logging
import os
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from common import get_homestak_root

logger = logging.getLogger(__name__)

# Certificate defaults
DEFAULT_CERT_DAYS = 365
DEFAULT_KEY_SIZE = 4096


def get_default_cert_dir() -> Path:
    """Return default TLS cert directory: $HOMESTAK_ROOT/config/tls/."""
    result: Path = get_homestak_root() / 'config' / 'tls'
    return result


@dataclass
class TLSConfig:
    """TLS configuration for the server."""

    cert_path: Path
    key_path: Path
    fingerprint: str

    @classmethod
    def from_paths(cls, cert_path: Path, key_path: Path) -> "TLSConfig":
        """Create config from existing certificate files.

        Args:
            cert_path: Path to certificate file
            key_path: Path to key file

        Returns:
            TLSConfig with computed fingerprint

        Raises:
            FileNotFoundError: If files don't exist
        """
        if not cert_path.exists():
            raise FileNotFoundError(f"Certificate not found: {cert_path}")
        if not key_path.exists():
            raise FileNotFoundError(f"Key not found: {key_path}")

        fingerprint = get_cert_fingerprint(cert_path)
        return cls(cert_path=cert_path, key_path=key_path, fingerprint=fingerprint)


def get_cert_fingerprint(cert_path: Path) -> str:
    """Get SHA256 fingerprint of a certificate.

    Args:
        cert_path: Path to PEM certificate file

    Returns:
        SHA256 fingerprint as hex string with colons (e.g., "AB:CD:EF:...")

    Raises:
        subprocess.CalledProcessError: If openssl command fails
    """
    result = subprocess.run(
        [
            "openssl", "x509",
            "-in", str(cert_path),
            "-noout",
            "-fingerprint",
            "-sha256"
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    # Output format: "sha256 Fingerprint=AB:CD:EF:..."
    # Extract just the fingerprint part
    output = result.stdout.strip()
    if "=" in output:
        return output.split("=", 1)[1]
    return output


def get_hostname() -> str:
    """Get the system hostname."""
    return socket.gethostname()


def get_primary_ip() -> Optional[str]:
    """Get the primary IP address.

    Uses the same approach as hostname -I: connects to external address
    and checks the bound local address.

    Returns:
        Primary IP address, or None if cannot be determined
    """
    try:
        # Connect to a public DNS server (doesn't actually send packets)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0)
        sock.connect(("8.8.8.8", 80))
        ip: str = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:
        return None


def generate_self_signed_cert(
    cert_dir: Optional[Path] = None,
    hostname: Optional[str] = None,
    days: int = DEFAULT_CERT_DAYS,
    key_size: int = DEFAULT_KEY_SIZE,
    force: bool = False,
) -> TLSConfig:
    """Generate a self-signed certificate for the server.

    Creates a certificate with:
    - CN = hostname
    - SAN = hostname + IP address (if available)
    - Key size = 4096 bits (secure default)
    - Validity = 365 days

    Args:
        cert_dir: Directory to store certificate files (default: $HOMESTAK_ROOT/config/tls/)
        hostname: Hostname for certificate CN and filename (default: system hostname)
        days: Certificate validity in days
        key_size: RSA key size in bits
        force: Overwrite existing certificate

    Returns:
        TLSConfig with paths and fingerprint

    Raises:
        FileExistsError: If certificate exists and force=False
        subprocess.CalledProcessError: If openssl command fails
        PermissionError: If cannot write to cert_dir
    """
    cert_dir = cert_dir or get_default_cert_dir()
    hostname = hostname or get_hostname()

    # Ensure directory exists
    cert_dir.mkdir(parents=True, exist_ok=True)

    cert_path = cert_dir / f"{hostname}.crt"
    key_path = cert_dir / f"{hostname}.key"

    # Check for existing certificate
    if cert_path.exists() and not force:
        logger.info("Using existing certificate: %s", cert_path)
        return TLSConfig.from_paths(cert_path, key_path)

    logger.info("Generating self-signed certificate for %s", hostname)

    # Build SAN (Subject Alternative Name) extensions
    san_entries = [f"DNS:{hostname}"]
    if ip := get_primary_ip():
        san_entries.append(f"IP:{ip}")

    # Create temporary config file for openssl
    with tempfile.NamedTemporaryFile(mode="w", suffix=".cnf", delete=False) as f:
        f.write(f"""
[req]
default_bits = {key_size}
prompt = no
default_md = sha256
distinguished_name = dn
x509_extensions = v3_ext

[dn]
CN = {hostname}

[v3_ext]
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = {",".join(san_entries)}
""")
        config_path = f.name

    try:
        # Generate key and certificate
        subprocess.run(
            [
                "openssl", "req",
                "-x509",
                "-nodes",
                "-newkey", f"rsa:{key_size}",
                "-keyout", str(key_path),
                "-out", str(cert_path),
                "-days", str(days),
                "-config", config_path,
            ],
            check=True,
            capture_output=True,
        )

        # Set restrictive permissions on key file
        os.chmod(key_path, 0o600)
        os.chmod(cert_path, 0o644)

    finally:
        # Clean up config file
        Path(config_path).unlink(missing_ok=True)

    fingerprint = get_cert_fingerprint(cert_path)
    logger.info("Certificate fingerprint (SHA256): %s", fingerprint)

    return TLSConfig(cert_path=cert_path, key_path=key_path, fingerprint=fingerprint)


def verify_cert_key_match(cert_path: Path, key_path: Path) -> bool:
    """Verify that a certificate and key match.

    Args:
        cert_path: Path to certificate file
        key_path: Path to key file

    Returns:
        True if certificate and key match
    """
    try:
        # Get modulus from certificate
        cert_result = subprocess.run(
            ["openssl", "x509", "-noout", "-modulus", "-in", str(cert_path)],
            capture_output=True,
            text=True,
            check=True,
        )
        cert_modulus = cert_result.stdout.strip()

        # Get modulus from key
        key_result = subprocess.run(
            ["openssl", "rsa", "-noout", "-modulus", "-in", str(key_path)],
            capture_output=True,
            text=True,
            check=True,
        )
        key_modulus = key_result.stdout.strip()

        return cert_modulus == key_modulus
    except subprocess.CalledProcessError:
        return False
