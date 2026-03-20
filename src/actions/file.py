"""File download and management actions."""

import logging
import time
from dataclasses import dataclass
from typing import Optional

from common import ActionResult, run_ssh, sudo_prefix
from config import HostConfig

logger = logging.getLogger(__name__)


@dataclass
class RemoveImageAction:
    """Remove packer image from PVE host."""
    name: str
    image_dir: str = '/var/lib/vz/template/iso'
    fail_if_missing: bool = False

    def run(self, config: HostConfig, _context: dict) -> ActionResult:
        """Remove image from PVE host."""
        start = time.time()

        pve_host = config.ssh_host
        user = config.automation_user
        sudo = sudo_prefix(user)
        image_name = config.packer_image.replace('.qcow2', '.img')
        image_path = f'{self.image_dir}/{image_name}'

        # Check if image exists
        logger.info(f"[{self.name}] Checking for {image_name} on {pve_host}...")
        rc, out, err = run_ssh(pve_host, f'{sudo}test -f {image_path} && echo exists',
                               user=user, timeout=30)

        if rc != 0 or 'exists' not in out:
            if self.fail_if_missing:
                return ActionResult(
                    success=False,
                    message=f"Image {image_name} not found",
                    duration=time.time() - start
                )
            return ActionResult(
                success=True,
                message=f"Image {image_name} already absent",
                duration=time.time() - start
            )

        # Remove image
        logger.info(f"[{self.name}] Removing {image_name} from {pve_host}...")
        rc, out, err = run_ssh(pve_host, f'{sudo}rm -f {image_path}', user=user, timeout=30)

        if rc != 0:
            return ActionResult(
                success=False,
                message=f"Failed to remove image: {err}",
                duration=time.time() - start
            )

        return ActionResult(
            success=True,
            message=f"Removed {image_name}",
            duration=time.time() - start
        )


@dataclass
class DownloadFileAction:
    """Download a file from a URL to a remote host."""
    name: str
    url: str
    dest_dir: str
    dest_filename: Optional[str] = None  # if None, use filename from URL
    host_key: str = 'node_ip'
    rename_ext: Optional[str] = None  # e.g., '.img' to rename .qcow2 files
    timeout: int = 300

    def run(self, config: HostConfig, context: dict) -> ActionResult:
        """Download file to remote host."""
        start = time.time()

        host = context.get(self.host_key)
        if not host:
            return ActionResult(
                success=False,
                message=f"No {self.host_key} in context",
                duration=time.time() - start
            )

        user = config.automation_user
        sudo = sudo_prefix(user)

        # Determine filename
        filename = self.dest_filename or self.url.split('/')[-1]
        dest = f"{self.dest_dir}/{filename}"

        # Create target directory
        logger.info(f"[{self.name}] Creating directory {self.dest_dir}...")
        rc, _, err = run_ssh(host, f'{sudo}mkdir -p {self.dest_dir}', user=user, timeout=30)
        if rc != 0:
            return ActionResult(
                success=False,
                message=f"Failed to create directory: {err}",
                duration=time.time() - start
            )

        # Download file
        logger.info(f"[{self.name}] Downloading {self.url}...")
        dl_cmd = f'{sudo}curl -fSL -o {dest} {self.url}'
        rc, _, err = run_ssh(host, dl_cmd, user=user, timeout=self.timeout)
        if rc != 0:
            return ActionResult(
                success=False,
                message=f"Failed to download from {self.url}: {err}",
                duration=time.time() - start
            )

        final_filename = filename

        # Rename extension if requested
        if self.rename_ext:
            # Find current extension
            if '.' in filename:
                base = filename.rsplit('.', 1)[0]
                new_filename = base + self.rename_ext
                rename_cmd = f'{sudo}mv {dest} {self.dest_dir}/{new_filename} 2>/dev/null || true'
                run_ssh(host, rename_cmd, user=user, timeout=30)
                final_filename = new_filename

        # Verify file exists
        verify_path = f"{self.dest_dir}/{final_filename}"
        rc, _, err = run_ssh(host, f'{sudo}ls -la {verify_path}', user=user, timeout=30)
        if rc != 0:
            return ActionResult(
                success=False,
                message=f"File not found after download: {err}",
                duration=time.time() - start
            )

        return ActionResult(
            success=True,
            message=f"Downloaded {final_filename}",
            duration=time.time() - start,
            context_updates={'downloaded_file': final_filename}
        )


@dataclass
class DownloadGitHubReleaseAction:
    """Download an asset from a GitHub release.

    Handles both single files and split files (e.g., large files split into
    .partaa, .partab, etc. due to GitHub's 2GB file size limit).
    """
    name: str
    asset_name: str  # e.g., "debian-12-custom.qcow2"
    dest_dir: str = '/var/lib/vz/template/iso'
    host_key: str = 'node_ip'
    rename_ext: Optional[str] = '.img'  # Proxmox convention
    timeout: int = 300

    def _get_split_parts(self, repo: str, tag: str, host: str,
                         user: str) -> list[str]:
        """Query GitHub API for split file parts matching asset_name.part*.

        Returns sorted list of part filenames (e.g., ['file.partaa', 'file.partab']).
        """
        api_url = f'https://api.github.com/repos/{repo}/releases/tags/{tag}'
        # Extract asset names matching our pattern
        pattern = self.asset_name + '.part'
        cmd = f"""curl -fsSL '{api_url}' | python3 -c "
import sys, json
data = json.load(sys.stdin)
assets = data.get('assets', [])
parts = [a['name'] for a in assets if a['name'].startswith('{pattern}')]
parts.sort()
print('\\n'.join(parts))
"
"""
        rc, out, _err = run_ssh(host, cmd, user=user, timeout=30)
        if rc == 0 and out.strip():
            return [p for p in out.strip().split('\n') if p]
        return []

    def _download_and_reassemble(self, repo: str, tag: str, parts: list[str],
                                  host: str, user: str, sudo: str,
                                  start: float) -> ActionResult:
        """Download split parts and reassemble into single file."""
        dest = f"{self.dest_dir}/{self.asset_name}"

        # Download each part
        for i, part in enumerate(parts):
            url = f'https://github.com/{repo}/releases/download/{tag}/{part}'
            part_dest = f"{self.dest_dir}/{part}"
            logger.info(f"[{self.name}] Downloading part {i+1}/{len(parts)}: {part}...")
            dl_cmd = f'{sudo}curl -fSL -o {part_dest} {url}'
            rc, _, err = run_ssh(host, dl_cmd, user=user, timeout=self.timeout)
            if rc != 0:
                # Clean up any downloaded parts on failure
                cleanup_cmd = f"{sudo}rm -f {self.dest_dir}/{self.asset_name}.part*"
                run_ssh(host, cleanup_cmd, user=user, timeout=30)
                return ActionResult(
                    success=False,
                    message=f"Failed to download part {part}: {err}",
                    duration=time.time() - start
                )

        # Reassemble parts (cat in sorted order)
        logger.info(f"[{self.name}] Reassembling {len(parts)} parts into {self.asset_name}...")
        # Use shell glob which sorts alphabetically (partaa, partab, etc.)
        reassemble_cmd = f"{sudo}sh -c 'cat {self.dest_dir}/{self.asset_name}.part* > {dest}'"
        rc, _, err = run_ssh(host, reassemble_cmd, user=user, timeout=120)
        if rc != 0:
            return ActionResult(
                success=False,
                message=f"Failed to reassemble parts: {err}",
                duration=time.time() - start
            )

        # Clean up parts
        cleanup_cmd = f"{sudo}rm -f {self.dest_dir}/{self.asset_name}.part*"
        run_ssh(host, cleanup_cmd, user=user, timeout=30)
        logger.info(f"[{self.name}] Cleaned up {len(parts)} part files")

        return ActionResult(success=True, message="", duration=0)  # Caller handles final result

    def run(self, config: HostConfig, context: dict) -> ActionResult:
        """Download release asset."""
        start = time.time()

        host = context.get(self.host_key)
        if not host:
            return ActionResult(
                success=False,
                message=f"No {self.host_key} in context",
                duration=time.time() - start
            )

        user = config.automation_user
        sudo = sudo_prefix(user)

        repo = config.image_release_repo
        tag = config.image_release

        url = f'https://github.com/{repo}/releases/download/{tag}/{self.asset_name}'
        dest = f"{self.dest_dir}/{self.asset_name}"

        # Create target directory
        logger.info(f"[{self.name}] Creating directory {self.dest_dir}...")
        rc, _, err = run_ssh(host, f'{sudo}mkdir -p {self.dest_dir}', user=user, timeout=30)
        if rc != 0:
            return ActionResult(
                success=False,
                message=f"Failed to create directory: {err}",
                duration=time.time() - start
            )

        # Try direct download first
        logger.info(f"[{self.name}] Downloading {self.asset_name} from {repo} release {tag}...")
        dl_cmd = f'{sudo}curl -fSL -o {dest} {url}'
        rc, _, err = run_ssh(host, dl_cmd, user=user, timeout=self.timeout)

        if rc != 0:
            # Check if file is split into parts
            logger.info(f"[{self.name}] Direct download failed, checking for split parts...")
            parts = self._get_split_parts(repo, tag, host, user)

            if parts:
                logger.info(f"[{self.name}] Found {len(parts)} split parts, downloading...")
                result = self._download_and_reassemble(repo, tag, parts, host, user, sudo, start)
                if not result.success:
                    return result
                # Continue to rename/verify below
            else:
                return ActionResult(
                    success=False,
                    message=f"Failed to download from {url} (no split parts found): {err}",
                    duration=time.time() - start
                )

        final_filename = self.asset_name

        # Rename extension if requested (e.g., .qcow2 -> .img)
        if self.rename_ext and '.' in self.asset_name:
            base = self.asset_name.rsplit('.', 1)[0]
            new_filename = base + self.rename_ext
            rename_cmd = f'{sudo}mv {dest} {self.dest_dir}/{new_filename} 2>/dev/null || true'
            run_ssh(host, rename_cmd, user=user, timeout=30)
            final_filename = new_filename

        # Verify file exists
        verify_path = f"{self.dest_dir}/{final_filename}"
        rc, _, err = run_ssh(host, f'{sudo}ls -la {verify_path}', user=user, timeout=30)
        if rc != 0:
            return ActionResult(
                success=False,
                message=f"File not found after download: {err}",
                duration=time.time() - start
            )

        return ActionResult(
            success=True,
            message=f"Downloaded {final_filename}",
            duration=time.time() - start,
            context_updates={'packer_image': final_filename}
        )
