"""Repo endpoint handler for the server.

Serves git repositories via HTTP dumb protocol with Bearer token auth.
Creates temporary bare repos with `_working` branch containing uncommitted changes.

TODO: Externalize timeout values (e.g., GIT_SHOW_TIMEOUT=5) for tuning.
See _serve_raw_file() subprocess calls.
"""

import logging
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, List, Dict, Tuple

from server.auth import validate_repo_token

logger = logging.getLogger(__name__)

# Known repos to serve
KNOWN_REPOS = ["bootstrap", "ansible", "iac-driver", "tofu", "packer", "config"]


class RepoManager:
    """Manages temporary bare repos for HTTP serving."""

    def __init__(
        self,
        repos_dir: Path,
        exclude_repos: Optional[List[str]] = None,
        extra_paths: Optional[Dict[str, Path]] = None,
    ):
        """Initialize repo manager.

        Args:
            repos_dir: Directory containing source repos
            exclude_repos: List of repo names to exclude
            extra_paths: Map of repo names to alternate paths (e.g., config at ~/config/)
        """
        self.repos_dir = repos_dir
        self.exclude_repos = set(exclude_repos or [])
        self.extra_paths = extra_paths or {}
        self.serve_dir: Optional[Path] = None
        self.repo_status: Dict[str, dict] = {}

    def prepare(self) -> Path:
        """Prepare bare repos for serving.

        Creates temporary directory with bare clones and _working branches.

        Returns:
            Path to serve directory

        Raises:
            RuntimeError: If no repos could be prepared
        """
        self.serve_dir = Path(tempfile.mkdtemp(prefix="server-repos-"))
        logger.info("Preparing repos in %s", self.serve_dir)

        for repo_name in KNOWN_REPOS:
            if repo_name in self.exclude_repos:
                self.repo_status[repo_name] = {"status": "excluded"}
                continue

            try:
                status = self._create_bare_repo(repo_name)
                self.repo_status[repo_name] = status
            except Exception as e:
                logger.warning("Failed to prepare %s: %s", repo_name, e)
                self.repo_status[repo_name] = {"status": "error", "error": str(e)}

        # Check at least one repo was prepared
        prepared = [k for k, v in self.repo_status.items() if v.get("status") == "ok"]
        if not prepared:
            raise RuntimeError("No repos could be prepared")

        logger.info("Prepared repos: %s", ", ".join(prepared))
        return self.serve_dir

    def cleanup(self):
        """Clean up temporary serve directory."""
        if self.serve_dir and self.serve_dir.exists():
            shutil.rmtree(self.serve_dir)
            logger.debug("Cleaned up %s", self.serve_dir)
            self.serve_dir = None

    def _create_bare_repo(self, repo_name: str) -> dict:
        """Create bare repo with _working branch.

        Args:
            repo_name: Repository name

        Returns:
            Status dict with status, uncommitted count

        Raises:
            FileNotFoundError: If source repo not found
            subprocess.CalledProcessError: If git commands fail
        """
        repo_path = self.extra_paths.get(repo_name, self.repos_dir / repo_name)
        assert self.serve_dir is not None  # Set by prepare() before calling this
        bare_path = self.serve_dir / f"{repo_name}.git"

        # Check source exists
        if not (repo_path / ".git").is_dir():
            raise FileNotFoundError(f"Not a git repo: {repo_path}")

        # Create bare clone
        subprocess.run(
            ["git", "clone", "--bare", "--quiet", str(repo_path), str(bare_path)],
            check=True,
            capture_output=True,
        )

        # Check for uncommitted changes
        result = subprocess.run(
            ["git", "-C", str(repo_path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
        changes = len(result.stdout.strip().splitlines()) if result.stdout else 0

        if changes > 0:
            # Create _working branch with working tree snapshot
            self._create_working_branch(repo_path, bare_path)
            logger.debug("%s: _working branch with %d uncommitted files", repo_name, changes)
        else:
            # Clean repo - _working points to HEAD
            head = subprocess.run(
                ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

            subprocess.run(
                ["git", "-C", str(bare_path), "update-ref", "refs/heads/_working", head],
                check=True,
                capture_output=True,
            )
            logger.debug("%s: _working branch (clean)", repo_name)

        # Set HEAD to _working so git clone gets uncommitted changes
        subprocess.run(
            ["git", "-C", str(bare_path), "symbolic-ref", "HEAD", "refs/heads/_working"],
            check=True,
            capture_output=True,
        )

        # Enable dumb HTTP protocol
        subprocess.run(
            ["git", "-C", str(bare_path), "update-server-info"],
            check=True,
            capture_output=True,
        )

        return {"status": "ok", "uncommitted": changes}

    def _create_working_branch(self, repo_path: Path, bare_path: Path):
        """Create _working branch with uncommitted changes.

        Uses git write-tree and commit-tree to create a commit
        containing the current working tree state.

        Args:
            repo_path: Path to source repo
            bare_path: Path to bare repo
        """
        git_dir = repo_path / ".git"
        index_backup = Path(tempfile.mktemp())

        try:
            # Backup index
            shutil.copy(git_dir / "index", index_backup)

            # Stage all changes
            subprocess.run(
                ["git", "-C", str(repo_path), "add", "-A"],
                check=True,
                capture_output=True,
            )

            # Create tree from index
            tree = subprocess.run(
                ["git", "-C", str(repo_path), "write-tree"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

            # Create commit
            head = subprocess.run(
                ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

            # Set author/committer identity for commit-tree — freshly bootstrapped
            # VMs may not have git user.name/user.email configured
            commit_env = {
                **os.environ,
                "GIT_AUTHOR_NAME": "homestak-server",
                "GIT_AUTHOR_EMAIL": "server@localhost",
                "GIT_COMMITTER_NAME": "homestak-server",
                "GIT_COMMITTER_EMAIL": "server@localhost",
            }
            commit = subprocess.run(
                ["git", "-C", str(repo_path), "commit-tree", tree, "-p", head, "-m",
                 "Working tree snapshot for server"],
                capture_output=True,
                text=True,
                check=True,
                env=commit_env,
            ).stdout.strip()

            # Restore index
            shutil.copy(index_backup, git_dir / "index")

            # Push commit to bare repo's _working branch
            subprocess.run(
                ["git", "-C", str(repo_path), "push", "--quiet",
                 str(bare_path), f"{commit}:refs/heads/_working"],
                check=True,
                capture_output=True,
            )

        finally:
            index_backup.unlink(missing_ok=True)


def handle_repo_request(
    path: str,
    auth_header: str,
    repo_token: str,
    serve_dir: Path,
) -> Tuple[bytes, int, str]:
    """Handle a repo request.

    Routes to either:
    - Git dumb HTTP protocol files (objects/, info/, refs/, etc.)
    - Raw file extraction from bare repo

    Args:
        path: Request path (e.g., "/bootstrap.git/install.sh")
        auth_header: Authorization header from request
        repo_token: Expected repo token
        serve_dir: Path to serve directory with bare repos

    Returns:
        Tuple of (content_bytes, http_status, content_type)
    """
    # Validate auth
    auth_error = validate_repo_token(auth_header, repo_token)
    if auth_error:
        error_body = _error_json(auth_error.code, auth_error.message)
        return error_body, auth_error.http_status, "application/json"

    # Parse path: /repo.git/...
    match = re.match(r"^/([^/]+\.git)/(.*)$", path)
    if not match:
        return _error_json("E100", f"Invalid path: {path}"), 400, "application/json"

    repo_name = match.group(1)
    file_path = match.group(2)

    repo_path = serve_dir / repo_name
    if not repo_path.is_dir():
        return _error_json("E200", f"Repository not found: {repo_name}"), 404, "application/json"

    # Check if this is a git protocol path or raw file request
    if _is_git_protocol_path(file_path):
        return _serve_git_file(repo_path, file_path)
    return _serve_raw_file(repo_path, file_path)


def _is_git_protocol_path(path: str) -> bool:
    """Check if path is a git dumb HTTP protocol path."""
    git_prefixes = ("objects/", "info/", "refs/", "packed-refs")
    git_files = ("HEAD", "config")
    return path.startswith(git_prefixes) or path in git_files


def _serve_git_file(repo_path: Path, file_path: str) -> Tuple[bytes, int, str]:
    """Serve a git protocol file from the bare repo.

    Args:
        repo_path: Path to bare repo
        file_path: Relative path within repo

    Returns:
        Tuple of (content_bytes, http_status, content_type)
    """
    full_path = repo_path / file_path
    if not full_path.exists():
        return _error_json("E200", f"File not found: {file_path}"), 404, "application/json"

    # Determine content type
    if file_path.startswith("objects/"):
        content_type = "application/x-git-loose-object"
        if file_path.startswith("objects/pack/"):
            if file_path.endswith(".pack"):
                content_type = "application/x-git-packed-objects"
            elif file_path.endswith(".idx"):
                content_type = "application/x-git-packed-objects-toc"
    else:
        content_type = "text/plain"

    content = full_path.read_bytes()
    return content, 200, content_type


def _serve_raw_file(repo_path: Path, file_path: str) -> Tuple[bytes, int, str]:
    """Serve a raw file extracted from the git repo.

    Uses `git show _working:{path}` to extract file content.

    Args:
        repo_path: Path to bare repo
        file_path: Path within the repo

    Returns:
        Tuple of (content_bytes, http_status, content_type)
    """
    try:
        # Try _working branch first
        result = subprocess.run(
            ["git", "-C", str(repo_path), "show", f"_working:{file_path}"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            # Try HEAD as fallback
            result = subprocess.run(
                ["git", "-C", str(repo_path), "show", f"HEAD:{file_path}"],
                capture_output=True,
                timeout=5,
                check=False,
            )
        if result.returncode != 0:
            return _error_json("E200", f"File not found: {file_path}"), 404, "application/json"

        content = result.stdout

        # Guess content type
        content_type, _ = mimetypes.guess_type(file_path)
        if content_type is None:
            if file_path.endswith(".sh"):
                content_type = "text/x-shellscript"
            elif file_path.endswith(".py"):
                content_type = "text/x-python"
            elif file_path.endswith((".yaml", ".yml")):
                content_type = "text/yaml"
            else:
                content_type = "application/octet-stream"

        return content, 200, content_type

    except subprocess.TimeoutExpired:
        return _error_json("E500", "Timeout extracting file"), 500, "application/json"
    except Exception as e:
        return _error_json("E500", f"Error: {e}"), 500, "application/json"


def _error_json(code: str, message: str) -> bytes:
    """Build JSON error response."""
    import json
    return json.dumps({"error": {"code": code, "message": message}}).encode("utf-8")
