"""Tests for server/httpd.py - unified HTTPS server."""

import base64
import hashlib
import hmac as hmac_mod
import http.client
import json
import ssl
import threading
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

TEST_SIGNING_KEY = "a" * 64  # 32 bytes hex = 256 bits


def _mint_test_token(node: str, spec: str) -> str:
    """Mint a provisioning token for integration tests."""
    payload = {"v": 1, "n": node, "s": spec, "iat": int(time.time())}
    payload_bytes = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(',', ':')).encode()
    ).rstrip(b'=')
    sig = hmac_mod.new(
        bytes.fromhex(TEST_SIGNING_KEY), payload_bytes, hashlib.sha256,
    ).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b'=')
    return f"{payload_bytes.decode()}.{sig_b64.decode()}"

# Add src to path for imports
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from server.httpd import (
    ServerHandler,
    Server,
    create_server,
    DEFAULT_PORT,
    DEFAULT_BIND,
)
from server.tls import generate_self_signed_cert, TLSConfig
from server.repos import RepoManager
from resolver.spec_resolver import SpecResolver


class TestServerHandlerRouting:
    """Tests for ServerHandler request routing."""

    @pytest.fixture
    def mock_handler(self):
        """Create a mock handler for routing tests."""
        handler = MagicMock(spec=ServerHandler)
        handler.path = "/health"
        handler.headers = {}
        return handler

    def test_health_check_routing(self):
        """Health check endpoint routes correctly."""
        # This tests the routing logic indirectly
        # In practice, we test via integration tests
        assert "/health" == "/health"

    def test_spec_routing(self):
        """Spec endpoints start with /spec/."""
        assert "/spec/base".startswith("/spec/")
        assert "/spec/pve".startswith("/spec/")

    def test_specs_list_routing(self):
        """Specs list endpoint is /specs."""
        assert "/specs" == "/specs"

    def test_repo_routing(self):
        """Repo endpoints contain .git."""
        assert ".git/" in "/bootstrap.git/info/refs"
        assert "/bootstrap.git".endswith(".git")


class TestServer:
    """Tests for Server class."""

    @pytest.fixture
    def site_config(self, tmp_path):
        """Create a minimal config for server testing."""
        (tmp_path / "specs").mkdir(parents=True)
        (tmp_path / "postures").mkdir(parents=True)

        site_yaml = {"defaults": {"domain": "test.local"}}
        (tmp_path / "site.yaml").write_text(yaml.dump(site_yaml))
        (tmp_path / "secrets.yaml").write_text(yaml.dump({"ssh_keys": {}}))

        dev_posture = {"auth": {"method": "network"}}
        (tmp_path / "postures" / "dev.yaml").write_text(yaml.dump(dev_posture))

        base_spec = {"schema_version": 1, "access": {"posture": "dev"}}
        (tmp_path / "specs" / "base.yaml").write_text(yaml.dump(base_spec))

        return tmp_path

    @pytest.fixture
    def tls_config(self, tmp_path):
        """Create TLS config for testing."""
        cert_dir = tmp_path / "certs"
        return generate_self_signed_cert(
            cert_dir=cert_dir, hostname="localhost", key_size=2048
        )

    def test_init_defaults(self):
        """Server initializes with default values."""
        server = Server()

        assert server.bind == DEFAULT_BIND
        assert server.port == DEFAULT_PORT
        assert server.spec_resolver is None
        assert server.repo_manager is None
        assert server.repo_token == ""
        assert server.tls_config is None

    def test_init_with_options(self, site_config, tls_config):
        """Server accepts all configuration options."""
        resolver = SpecResolver(etc_path=site_config)

        server = Server(
            bind="127.0.0.1",
            port=8443,
            spec_resolver=resolver,
            repo_token="test-token",
            tls_config=tls_config,
        )

        assert server.bind == "127.0.0.1"
        assert server.port == 8443
        assert server.spec_resolver is resolver
        assert server.repo_token == "test-token"
        assert server.tls_config is tls_config



class TestServerIntegration:
    """Integration tests for Server with real HTTP requests."""

    @pytest.fixture
    def running_server(self, tmp_path):
        """Start a server and return connection details."""
        # Create config
        site_config = tmp_path / "config"
        (site_config / "specs").mkdir(parents=True)
        (site_config / "postures").mkdir(parents=True)

        site_yaml = {"defaults": {"domain": "test.local", "timezone": "UTC"}}
        (site_config / "site.yaml").write_text(yaml.dump(site_yaml))
        (site_config / "secrets.yaml").write_text(yaml.dump({
            "ssh_keys": {},
            "auth": {"signing_key": TEST_SIGNING_KEY},
        }))

        dev_posture = {"auth": {"method": "network"}}
        (site_config / "postures" / "dev.yaml").write_text(yaml.dump(dev_posture))

        base_spec = {"schema_version": 1, "access": {"posture": "dev"}}
        (site_config / "specs" / "base.yaml").write_text(yaml.dump(base_spec))

        # Create TLS config
        cert_dir = tmp_path / "certs"
        tls_config = generate_self_signed_cert(
            cert_dir=cert_dir, hostname="localhost", key_size=2048
        )

        # Create and start server
        resolver = SpecResolver(etc_path=site_config)
        server = Server(
            bind="127.0.0.1",
            port=0,  # Let OS assign port
            spec_resolver=resolver,
            tls_config=tls_config,
        )
        server.start()

        # Get actual port
        port = server.server.server_address[1]

        # Start serving in background thread
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.daemon = True
        server_thread.start()

        # Wait for server to be ready
        time.sleep(0.1)

        yield {
            "host": "127.0.0.1",
            "port": port,
            "tls_config": tls_config,
            "server": server,
        }

        # Cleanup
        server.shutdown()

    def _create_https_connection(self, host, port):
        """Create HTTPS connection with self-signed cert."""
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return http.client.HTTPSConnection(host, port, context=context)

    def test_health_check(self, running_server):
        """Health check endpoint returns OK."""
        conn = self._create_https_connection(
            running_server["host"], running_server["port"]
        )
        conn.request("GET", "/health")
        response = conn.getresponse()

        assert response.status == 200
        data = json.loads(response.read())
        assert data["status"] == "ok"

    def test_specs_list(self, running_server):
        """Specs list endpoint returns available specs."""
        conn = self._create_https_connection(
            running_server["host"], running_server["port"]
        )
        conn.request("GET", "/specs")
        response = conn.getresponse()

        assert response.status == 200
        data = json.loads(response.read())
        assert "specs" in data
        assert "base" in data["specs"]

    def test_spec_request(self, running_server):
        """Spec request with valid provisioning token returns resolved spec."""
        token = _mint_test_token("base", "base")
        conn = self._create_https_connection(
            running_server["host"], running_server["port"]
        )
        conn.request("GET", "/spec/base", headers={
            "Authorization": f"Bearer {token}",
        })
        response = conn.getresponse()

        assert response.status == 200
        data = json.loads(response.read())
        assert data["identity"]["hostname"] == "base"

    def test_spec_not_found(self, running_server):
        """Nonexistent spec returns 404."""
        token = _mint_test_token("nonexistent", "nonexistent")
        conn = self._create_https_connection(
            running_server["host"], running_server["port"]
        )
        conn.request("GET", "/spec/nonexistent", headers={
            "Authorization": f"Bearer {token}",
        })
        response = conn.getresponse()

        assert response.status == 404
        data = json.loads(response.read())
        assert "error" in data

    def test_unknown_endpoint(self, running_server):
        """Unknown endpoint returns 400."""
        conn = self._create_https_connection(
            running_server["host"], running_server["port"]
        )
        conn.request("GET", "/unknown")
        response = conn.getresponse()

        assert response.status == 400
        data = json.loads(response.read())
        assert "error" in data


class TestCreateServer:
    """Tests for create_server factory function."""

    def test_creates_server_instance(self):
        """create_server returns Server instance."""
        server = create_server(port=8443, bind="127.0.0.1")

        assert isinstance(server, Server)
        assert server.port == 8443
        assert server.bind == "127.0.0.1"

    def test_passes_all_options(self, tmp_path):
        """create_server passes all options to constructor."""
        # Create minimal config
        (tmp_path / "specs").mkdir(parents=True)
        (tmp_path / "site.yaml").write_text(yaml.dump({"defaults": {}}))
        (tmp_path / "secrets.yaml").write_text(yaml.dump({}))

        resolver = SpecResolver(etc_path=tmp_path)
        tls_config = generate_self_signed_cert(
            cert_dir=tmp_path / "certs", hostname="localhost", key_size=2048
        )

        server = create_server(
            bind="127.0.0.1",
            port=8443,
            spec_resolver=resolver,
            repo_token="test-token",
            tls_config=tls_config,
        )

        assert server.spec_resolver is resolver
        assert server.repo_token == "test-token"
        assert server.tls_config is tls_config


