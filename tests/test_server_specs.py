"""Tests for server/specs.py - spec endpoint handler."""

from unittest.mock import MagicMock, patch

import pytest
import yaml

from conftest import TEST_SIGNING_KEY, mint_test_token
from server.specs import handle_spec_request, handle_specs_list, _error_response
from resolver.spec_resolver import SpecResolver, SpecNotFoundError, SchemaValidationError
from resolver.base import PostureNotFoundError, SSHKeyNotFoundError, ResolverError


class TestErrorResponse:
    """Tests for _error_response helper."""

    def test_error_response_format(self):
        """Error response has correct structure."""
        response = _error_response("E200", "Not found")
        assert response == {"error": {"code": "E200", "message": "Not found"}}


class TestHandleSpecRequest:
    """Tests for handle_spec_request function."""

    @pytest.fixture
    def site_config(self, tmp_path):
        """Create a minimal config for spec testing."""
        # Create directories
        (tmp_path / "specs").mkdir(parents=True)
        (tmp_path / "postures").mkdir(parents=True)

        # Create site.yaml
        site_yaml = {
            "defaults": {
                "timezone": "America/Denver",
                "domain": "example.com",
            }
        }
        (tmp_path / "site.yaml").write_text(yaml.dump(site_yaml))

        # Create secrets.yaml
        secrets_yaml = {
            "ssh_keys": {
                "admin": "ssh-ed25519 AAAA... admin@host",
            },
            "auth": {
                "signing_key": TEST_SIGNING_KEY,
            },
        }
        (tmp_path / "secrets.yaml").write_text(yaml.dump(secrets_yaml))

        # Create postures
        dev_posture = {"auth": {"method": "network"}, "ssh": {"port": 22}}
        (tmp_path / "postures" / "dev.yaml").write_text(yaml.dump(dev_posture))

        # Create specs
        base_spec = {
            "schema_version": 1,
            "access": {
                "posture": "dev",
                "users": [{"name": "root", "ssh_keys": ["admin"]}],
            },
            "platform": {"packages": ["htop"]},
        }
        (tmp_path / "specs" / "base.yaml").write_text(yaml.dump(base_spec))

        return tmp_path

    @pytest.fixture
    def resolver(self, site_config):
        """Create SpecResolver with test config."""
        return SpecResolver(etc_path=site_config)

    def test_success_returns_spec(self, resolver):
        """Successful request returns resolved spec."""
        token = mint_test_token("edge", "base")
        response, status = handle_spec_request(
            "edge", f"Bearer {token}", resolver, TEST_SIGNING_KEY
        )

        assert status == 200
        assert response["schema_version"] == 1
        assert response["identity"]["hostname"] == "base"
        assert response["identity"]["domain"] == "example.com"

    def test_success_resolves_ssh_keys(self, resolver):
        """Successful request includes resolved SSH keys."""
        token = mint_test_token("edge", "base")
        response, status = handle_spec_request(
            "edge", f"Bearer {token}", resolver, TEST_SIGNING_KEY
        )

        assert status == 200
        users = response["access"]["users"]
        assert len(users) == 1
        assert users[0]["ssh_keys"][0].startswith("ssh-ed25519")

    def test_success_removes_internal_posture(self, resolver):
        """Response does not include internal _posture field."""
        token = mint_test_token("edge", "base")
        response, status = handle_spec_request(
            "edge", f"Bearer {token}", resolver, TEST_SIGNING_KEY
        )

        assert status == 200
        assert "_posture" not in response.get("access", {})

    def test_missing_token_returns_400(self, resolver):
        """Missing provisioning token returns 400."""
        response, status = handle_spec_request(
            "edge", "", resolver, TEST_SIGNING_KEY
        )

        assert status == 400
        assert "error" in response
        assert response["error"]["code"] == "E300"

    def test_invalid_token_returns_401(self, resolver):
        """Invalid provisioning token returns 401."""
        wrong_key = "b" * 64
        token = mint_test_token("edge", "base", signing_key=wrong_key)
        response, status = handle_spec_request(
            "edge", f"Bearer {token}", resolver, TEST_SIGNING_KEY
        )

        assert status == 401
        assert response["error"]["code"] == "E301"

    def test_missing_signing_key_returns_500(self, resolver):
        """Missing signing key returns 500."""
        token = mint_test_token("edge", "base")
        response, status = handle_spec_request(
            "edge", f"Bearer {token}", resolver, ""
        )

        assert status == 500
        assert response["error"]["code"] == "E500"

    def test_spec_not_found_returns_404(self, resolver):
        """Token references nonexistent spec returns 404."""
        token = mint_test_token("edge", "nonexistent")
        response, status = handle_spec_request(
            "edge", f"Bearer {token}", resolver, TEST_SIGNING_KEY
        )

        assert status == 404
        assert response["error"]["code"] == "E200"

    def test_posture_not_found_returns_404(self, site_config):
        """Bad posture FK in spec returns 404."""
        bad_spec = {"schema_version": 1, "access": {"posture": "nonexistent"}}
        (site_config / "specs" / "bad.yaml").write_text(yaml.dump(bad_spec))

        resolver = SpecResolver(etc_path=site_config)
        token = mint_test_token("edge", "bad")
        response, status = handle_spec_request(
            "edge", f"Bearer {token}", resolver, TEST_SIGNING_KEY
        )

        assert status == 404
        assert response["error"]["code"] == "E201"

    def test_ssh_key_not_found_returns_404(self, site_config):
        """Bad SSH key FK returns 404."""
        bad_spec = {
            "schema_version": 1,
            "access": {
                "posture": "dev",
                "users": [{"name": "root", "ssh_keys": ["nonexistent"]}],
            },
        }
        (site_config / "specs" / "bad-ssh.yaml").write_text(yaml.dump(bad_spec))

        resolver = SpecResolver(etc_path=site_config)
        token = mint_test_token("edge", "bad-ssh")
        response, status = handle_spec_request(
            "edge", f"Bearer {token}", resolver, TEST_SIGNING_KEY
        )

        assert status == 404
        assert response["error"]["code"] == "E202"

    def test_spec_resolved_from_token_claim(self, site_config):
        """Spec is resolved using token's 's' claim, not URL identity."""
        resolver = SpecResolver(etc_path=site_config)
        # Token says spec=base, URL says identity=edge
        token = mint_test_token("edge", "base")
        response, status = handle_spec_request(
            "edge", f"Bearer {token}", resolver, TEST_SIGNING_KEY
        )

        assert status == 200
        # Spec resolved from "base", hostname set from base spec
        assert response["identity"]["hostname"] == "base"

    def test_internal_error_returns_500(self, resolver):
        """Unexpected error returns 500."""
        token = mint_test_token("edge", "base")
        with patch.object(resolver, "resolve", side_effect=RuntimeError("Boom")):
            response, status = handle_spec_request(
                "edge", f"Bearer {token}", resolver, TEST_SIGNING_KEY
            )

        assert status == 500
        assert response["error"]["code"] == "E500"
        assert "Internal error" in response["error"]["message"]


class TestHandleSpecsList:
    """Tests for handle_specs_list function."""

    @pytest.fixture
    def site_config(self, tmp_path):
        """Create a minimal config with multiple specs."""
        (tmp_path / "specs").mkdir(parents=True)
        (tmp_path / "postures").mkdir(parents=True)
        (tmp_path / "site.yaml").write_text(yaml.dump({"defaults": {}}))
        (tmp_path / "secrets.yaml").write_text(yaml.dump({}))

        # Create postures
        (tmp_path / "postures" / "dev.yaml").write_text(
            yaml.dump({"auth": {"method": "network"}})
        )

        # Create multiple specs
        for name in ["base", "pve", "k8s", "staging"]:
            spec = {"schema_version": 1, "access": {"posture": "dev"}}
            (tmp_path / "specs" / f"{name}.yaml").write_text(yaml.dump(spec))

        return tmp_path

    @pytest.fixture
    def resolver(self, site_config):
        """Create SpecResolver with test config."""
        return SpecResolver(etc_path=site_config)

    def test_list_specs_returns_all(self, resolver):
        """Lists all available specs."""
        response, status = handle_specs_list(resolver)

        assert status == 200
        assert "specs" in response
        specs = response["specs"]
        assert "base" in specs
        assert "pve" in specs
        assert "k8s" in specs
        assert "staging" in specs

    def test_list_specs_sorted(self, resolver):
        """Specs are returned in sorted order."""
        response, status = handle_specs_list(resolver)

        assert status == 200
        specs = response["specs"]
        assert specs == sorted(specs)

    def test_list_specs_empty_dir(self, tmp_path):
        """Returns empty list when no specs exist."""
        resolver = SpecResolver(etc_path=tmp_path)
        response, status = handle_specs_list(resolver)

        assert status == 200
        assert response["specs"] == []

    def test_list_specs_internal_error(self, resolver):
        """Unexpected error returns 500."""
        with patch.object(resolver, "list_specs", side_effect=RuntimeError("Boom")):
            response, status = handle_specs_list(resolver)

        assert status == 500
        assert response["error"]["code"] == "E500"
