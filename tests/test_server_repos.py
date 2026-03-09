"""Tests for server/repos.py - repo endpoint handler."""

import json
import os
import subprocess
import tempfile
from unittest.mock import patch, MagicMock

import pytest

from server.repos import (
    RepoManager,
    handle_repo_request,
    KNOWN_REPOS,
    _is_git_protocol_path,
    _serve_git_file,
    _serve_raw_file,
    _error_json,
)


class TestErrorJson:
    """Tests for _error_json helper."""

    def test_error_json_format(self):
        """Error JSON has correct structure."""
        result = _error_json("E200", "Not found")
        data = json.loads(result.decode())
        assert data == {"error": {"code": "E200", "message": "Not found"}}


class TestIsGitProtocolPath:
    """Tests for _is_git_protocol_path function."""

    def test_objects_path(self):
        """objects/ paths are git protocol."""
        assert _is_git_protocol_path("objects/pack/pack-123.pack") is True
        assert _is_git_protocol_path("objects/ab/1234567890") is True

    def test_info_path(self):
        """info/ paths are git protocol."""
        assert _is_git_protocol_path("info/refs") is True
        assert _is_git_protocol_path("info/packs") is True

    def test_refs_path(self):
        """refs/ paths are git protocol."""
        assert _is_git_protocol_path("refs/heads/master") is True
        assert _is_git_protocol_path("refs/tags/v1.0") is True

    def test_special_files(self):
        """HEAD and config are git protocol."""
        assert _is_git_protocol_path("HEAD") is True
        assert _is_git_protocol_path("config") is True
        assert _is_git_protocol_path("packed-refs") is True

    def test_regular_files(self):
        """Regular files are not git protocol."""
        assert _is_git_protocol_path("README.md") is False
        assert _is_git_protocol_path("src/main.py") is False
        assert _is_git_protocol_path("install.sh") is False


class TestRepoManager:
    """Tests for RepoManager class."""

    @pytest.fixture
    def repos_dir(self, tmp_path):
        """Create a minimal repos directory structure."""
        # Create a few fake git repos
        for repo_name in ["bootstrap", "ansible"]:
            repo_path = tmp_path / repo_name
            repo_path.mkdir()

            # Initialize git repo
            subprocess.run(
                ["git", "init", "--quiet"],
                cwd=repo_path,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=repo_path,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test User"],
                cwd=repo_path,
                check=True,
            )

            # Create a file and commit
            (repo_path / "README.md").write_text(f"# {repo_name}\n")
            subprocess.run(["git", "add", "."], cwd=repo_path, check=True)
            subprocess.run(
                ["git", "commit", "-m", "Initial commit", "--quiet"],
                cwd=repo_path,
                check=True,
            )

        return tmp_path

    def test_init_with_defaults(self, repos_dir):
        """RepoManager initializes with default values."""
        manager = RepoManager(repos_dir=repos_dir)

        assert manager.repos_dir == repos_dir
        assert manager.exclude_repos == set()
        assert manager.serve_dir is None
        assert manager.repo_status == {}

    def test_init_with_excludes(self, repos_dir):
        """RepoManager accepts exclude list."""
        manager = RepoManager(repos_dir=repos_dir, exclude_repos=["packer", "tofu"])

        assert manager.exclude_repos == {"packer", "tofu"}

    def test_prepare_creates_serve_dir(self, repos_dir):
        """prepare creates temporary serve directory."""
        manager = RepoManager(repos_dir=repos_dir)
        serve_dir = manager.prepare()

        assert serve_dir is not None
        assert serve_dir.exists()
        assert serve_dir.is_dir()

    def test_prepare_creates_bare_repos(self, repos_dir):
        """prepare creates bare clones of repos."""
        manager = RepoManager(repos_dir=repos_dir)
        serve_dir = manager.prepare()

        # Check bootstrap.git exists
        bootstrap_git = serve_dir / "bootstrap.git"
        assert bootstrap_git.exists()
        assert (bootstrap_git / "HEAD").exists()  # Bare repo indicator

    def test_prepare_creates_working_branch(self, repos_dir):
        """prepare creates _working branch."""
        manager = RepoManager(repos_dir=repos_dir)
        serve_dir = manager.prepare()

        bootstrap_git = serve_dir / "bootstrap.git"

        # Check _working branch exists
        result = subprocess.run(
            ["git", "-C", str(bootstrap_git), "branch", "--list", "_working"],
            capture_output=True,
            text=True,
        )
        assert "_working" in result.stdout

    def test_prepare_working_branch_with_uncommitted(self, repos_dir):
        """prepare captures uncommitted changes in _working branch."""
        # Add uncommitted file
        uncommitted_file = repos_dir / "bootstrap" / "uncommitted.txt"
        uncommitted_file.write_text("uncommitted content\n")

        manager = RepoManager(repos_dir=repos_dir)
        serve_dir = manager.prepare()

        # Check file is available via _working branch
        result = subprocess.run(
            [
                "git", "-C", str(serve_dir / "bootstrap.git"),
                "show", "_working:uncommitted.txt"
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "uncommitted content" in result.stdout

    def test_prepare_excludes_repos(self, repos_dir):
        """prepare skips excluded repos."""
        manager = RepoManager(repos_dir=repos_dir, exclude_repos=["bootstrap"])
        serve_dir = manager.prepare()

        assert not (serve_dir / "bootstrap.git").exists()
        assert (serve_dir / "ansible.git").exists()
        assert manager.repo_status.get("bootstrap", {}).get("status") == "excluded"

    def test_prepare_tracks_status(self, repos_dir):
        """prepare tracks repo status."""
        manager = RepoManager(repos_dir=repos_dir)
        manager.prepare()

        assert "bootstrap" in manager.repo_status
        assert manager.repo_status["bootstrap"]["status"] == "ok"

    def test_prepare_enables_dumb_protocol(self, repos_dir):
        """prepare runs git update-server-info."""
        manager = RepoManager(repos_dir=repos_dir)
        serve_dir = manager.prepare()

        # info/refs should exist after update-server-info
        info_refs = serve_dir / "bootstrap.git" / "info" / "refs"
        assert info_refs.exists()

    def test_cleanup_removes_serve_dir(self, repos_dir):
        """cleanup removes temporary directory."""
        manager = RepoManager(repos_dir=repos_dir)
        serve_dir = manager.prepare()

        assert serve_dir.exists()

        manager.cleanup()

        assert not serve_dir.exists()
        assert manager.serve_dir is None

    def test_cleanup_idempotent(self, repos_dir):
        """cleanup can be called multiple times."""
        manager = RepoManager(repos_dir=repos_dir)
        manager.prepare()

        manager.cleanup()
        manager.cleanup()  # Should not raise


class TestHandleRepoRequest:
    """Tests for handle_repo_request function."""

    @pytest.fixture
    def serve_dir(self, tmp_path):
        """Create a minimal serve directory with bare repo."""
        # Create a bare repo
        repo_path = tmp_path / "test.git"
        subprocess.run(["git", "init", "--bare", str(repo_path)], check=True, capture_output=True)

        # Create a temp working copy to add content
        work_dir = tmp_path / "work"
        subprocess.run(["git", "clone", str(repo_path), str(work_dir)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(work_dir), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(work_dir), "config", "user.name", "Test"], check=True)

        # Add test files
        (work_dir / "README.md").write_text("# Test\n")
        (work_dir / "install.sh").write_text("#!/bin/bash\necho 'hello'\n")
        (work_dir / "config.yaml").write_text("key: value\n")
        subprocess.run(["git", "-C", str(work_dir), "add", "."], check=True)
        subprocess.run(["git", "-C", str(work_dir), "commit", "-m", "Initial"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(work_dir), "push"], check=True, capture_output=True)

        # Create _working branch
        subprocess.run(
            ["git", "-C", str(repo_path), "branch", "_working", "HEAD"],
            check=True,
            capture_output=True,
        )

        # Update server info
        subprocess.run(
            ["git", "-C", str(repo_path), "update-server-info"],
            check=True,
            capture_output=True,
        )

        return tmp_path

    def test_auth_required(self, serve_dir):
        """Request without token fails with 401."""
        content, status, content_type = handle_repo_request(
            "/test.git/info/refs",
            "",
            "expected-token",
            serve_dir,
        )

        assert status == 401
        assert content_type == "application/json"
        data = json.loads(content)
        assert data["error"]["code"] == "E300"

    def test_auth_success(self, serve_dir):
        """Request with valid token succeeds."""
        content, status, content_type = handle_repo_request(
            "/test.git/info/refs",
            "Bearer expected-token",
            "expected-token",
            serve_dir,
        )

        assert status == 200

    def test_invalid_path_format(self, serve_dir):
        """Invalid path format returns 400."""
        content, status, content_type = handle_repo_request(
            "/not-a-repo/file",
            "Bearer token",
            "token",
            serve_dir,
        )

        assert status == 400
        assert content_type == "application/json"

    def test_repo_not_found(self, serve_dir):
        """Nonexistent repo returns 404."""
        content, status, content_type = handle_repo_request(
            "/nonexistent.git/info/refs",
            "Bearer token",
            "token",
            serve_dir,
        )

        assert status == 404
        assert content_type == "application/json"
        data = json.loads(content)
        assert "not found" in data["error"]["message"].lower()

    def test_git_protocol_file(self, serve_dir):
        """Git protocol file is served."""
        content, status, content_type = handle_repo_request(
            "/test.git/info/refs",
            "Bearer token",
            "token",
            serve_dir,
        )

        assert status == 200
        assert content_type == "text/plain"
        assert len(content) > 0

    def test_git_protocol_head(self, serve_dir):
        """HEAD file is served."""
        content, status, content_type = handle_repo_request(
            "/test.git/HEAD",
            "Bearer token",
            "token",
            serve_dir,
        )

        assert status == 200
        assert b"ref:" in content or content.startswith(b"ref:")

    def test_raw_file_from_working(self, serve_dir):
        """Raw file is served from _working branch."""
        content, status, content_type = handle_repo_request(
            "/test.git/README.md",
            "Bearer token",
            "token",
            serve_dir,
        )

        assert status == 200
        assert b"# Test" in content

    def test_raw_file_shell_script_type(self, serve_dir):
        """Shell script has correct content type."""
        content, status, content_type = handle_repo_request(
            "/test.git/install.sh",
            "Bearer token",
            "token",
            serve_dir,
        )

        assert status == 200
        # Python mimetypes returns text/x-sh for .sh files
        assert content_type == "text/x-sh"

    def test_raw_file_yaml_type(self, serve_dir):
        """YAML file has correct content type."""
        content, status, content_type = handle_repo_request(
            "/test.git/config.yaml",
            "Bearer token",
            "token",
            serve_dir,
        )

        assert status == 200
        # Both are valid YAML MIME types; system mimetypes DB varies
        assert content_type in ("text/yaml", "application/yaml")

    def test_raw_file_not_found(self, serve_dir):
        """Nonexistent raw file returns 404."""
        content, status, content_type = handle_repo_request(
            "/test.git/nonexistent.txt",
            "Bearer token",
            "token",
            serve_dir,
        )

        assert status == 404
        assert content_type == "application/json"

    def test_dev_mode_no_token(self, serve_dir):
        """Dev mode (empty expected_token) accepts any request."""
        content, status, content_type = handle_repo_request(
            "/test.git/README.md",
            "",
            "",  # Dev mode: no token required
            serve_dir,
        )

        assert status == 200


class TestServeGitFile:
    """Tests for _serve_git_file function."""

    @pytest.fixture
    def bare_repo(self, tmp_path):
        """Create a bare repo with content."""
        repo_path = tmp_path / "test.git"
        subprocess.run(["git", "init", "--bare", str(repo_path)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo_path), "update-server-info"],
            check=True,
            capture_output=True,
        )
        return repo_path

    def test_file_not_found(self, bare_repo):
        """Returns 404 for missing file."""
        content, status, content_type = _serve_git_file(bare_repo, "nonexistent")

        assert status == 404
        assert content_type == "application/json"

    def test_pack_file_content_type(self, bare_repo):
        """Pack files have correct content type."""
        # Create a pack file (empty is fine for content-type test)
        pack_dir = bare_repo / "objects" / "pack"
        pack_dir.mkdir(parents=True, exist_ok=True)
        pack_file = pack_dir / "pack-123.pack"
        pack_file.write_bytes(b"PACK")

        content, status, content_type = _serve_git_file(bare_repo, "objects/pack/pack-123.pack")

        assert content_type == "application/x-git-packed-objects"

    def test_idx_file_content_type(self, bare_repo):
        """Index files have correct content type."""
        pack_dir = bare_repo / "objects" / "pack"
        pack_dir.mkdir(parents=True, exist_ok=True)
        idx_file = pack_dir / "pack-123.idx"
        idx_file.write_bytes(b"\xff\x74\x4f\x63")  # Git idx magic

        content, status, content_type = _serve_git_file(bare_repo, "objects/pack/pack-123.idx")

        assert content_type == "application/x-git-packed-objects-toc"


class TestServeRawFile:
    """Tests for _serve_raw_file function."""

    @pytest.fixture
    def repo_with_content(self, tmp_path):
        """Create a bare repo with files."""
        repo_path = tmp_path / "test.git"
        work_path = tmp_path / "work"

        subprocess.run(["git", "init", "--bare", str(repo_path)], check=True, capture_output=True)
        subprocess.run(["git", "clone", str(repo_path), str(work_path)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(work_path), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(work_path), "config", "user.name", "Test"], check=True)

        # Create files with various extensions
        (work_path / "test.py").write_text("print('hello')\n")
        (work_path / "data.json").write_text('{"key": "value"}\n')
        (work_path / "unknown.qwerty").write_bytes(b"\x00\x01\x02")

        subprocess.run(["git", "-C", str(work_path), "add", "."], check=True)
        subprocess.run(["git", "-C", str(work_path), "commit", "-m", "Files"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(work_path), "push"], check=True, capture_output=True)

        # Create _working branch
        subprocess.run(
            ["git", "-C", str(repo_path), "branch", "_working", "HEAD"],
            check=True,
            capture_output=True,
        )

        return repo_path

    def test_python_file_content_type(self, repo_with_content):
        """Python file has correct content type."""
        content, status, content_type = _serve_raw_file(repo_with_content, "test.py")

        assert status == 200
        assert content_type == "text/x-python"

    def test_json_file_content_type(self, repo_with_content):
        """JSON file has correct content type."""
        content, status, content_type = _serve_raw_file(repo_with_content, "data.json")

        assert status == 200
        assert content_type == "application/json"

    def test_unknown_extension_type(self, repo_with_content):
        """Unknown extension falls back to octet-stream."""
        content, status, content_type = _serve_raw_file(repo_with_content, "unknown.qwerty")

        assert status == 200
        assert content_type == "application/octet-stream"

    def test_file_not_found(self, repo_with_content):
        """Nonexistent file returns 404."""
        content, status, content_type = _serve_raw_file(repo_with_content, "nonexistent.txt")

        assert status == 404
        assert content_type == "application/json"
