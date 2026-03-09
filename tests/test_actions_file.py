"""Tests for file download action classes.

Tests for DownloadGitHubReleaseAction including split file handling.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))


class TestDownloadGitHubReleaseAction:
    """Test DownloadGitHubReleaseAction including split file handling."""

    def test_direct_download_success(self):
        """Direct download of single file should succeed."""
        from actions.file import DownloadGitHubReleaseAction

        action = DownloadGitHubReleaseAction(
            name='test',
            asset_name='debian-12.qcow2',
            host_key='node_ip'
        )

        config = MagicMock()
        config.packer_release_repo = 'homestak-iac/packer'
        config.packer_release = 'v0.20'
        context = {'node_ip': '192.0.2.1'}

        with patch('actions.file.run_ssh') as mock_ssh:
            # mkdir success, download success, mv rename, verify success
            mock_ssh.side_effect = [
                (0, '', ''),      # mkdir
                (0, '', ''),      # curl download
                (0, '', ''),      # mv rename (rename_ext='.img' by default)
                (0, '-rw-r--r-- 1 root root 123456 file', ''),  # ls verify
            ]
            result = action.run(config, context)

        assert result.success is True
        assert 'Downloaded' in result.message

    def test_missing_host_key_returns_error(self):
        """Missing host_key in context should return failure."""
        from actions.file import DownloadGitHubReleaseAction

        action = DownloadGitHubReleaseAction(
            name='test',
            asset_name='test.qcow2',
            host_key='nonexistent'
        )

        config = MagicMock()
        config.packer_release_repo = 'homestak-iac/packer'
        config.packer_release = 'v0.20'
        context = {}

        result = action.run(config, context)

        assert result.success is False
        assert 'nonexistent' in result.message

    def test_split_file_detection_and_download(self):
        """Should detect split files and download/reassemble them."""
        from actions.file import DownloadGitHubReleaseAction

        action = DownloadGitHubReleaseAction(
            name='test',
            asset_name='pve-9.qcow2',
            host_key='node_ip'
        )

        config = MagicMock()
        config.packer_release_repo = 'homestak-iac/packer'
        config.packer_release = 'v0.20'
        context = {'node_ip': '192.0.2.1'}

        with patch('actions.file.run_ssh') as mock_ssh:
            mock_ssh.side_effect = [
                (0, '', ''),      # mkdir
                (1, '', 'curl: (22) 404'),  # direct download fails
                # _get_split_parts returns parts
                (0, 'pve-9.qcow2.partaa\npve-9.qcow2.partab\n', ''),
                (0, '', ''),      # download partaa
                (0, '', ''),      # download partab
                (0, '', ''),      # cat reassemble
                (0, '', ''),      # rm cleanup
                (0, '', ''),      # mv rename (rename_ext='.img' by default)
                (0, '-rw-r--r-- 1 root root 123456 file', ''),  # ls verify
            ]
            result = action.run(config, context)

        assert result.success is True
        assert 'Downloaded' in result.message

    def test_split_file_part_download_failure(self):
        """Failure to download a part should clean up and return error."""
        from actions.file import DownloadGitHubReleaseAction

        action = DownloadGitHubReleaseAction(
            name='test',
            asset_name='pve-9.qcow2',
            host_key='node_ip'
        )

        config = MagicMock()
        config.packer_release_repo = 'homestak-iac/packer'
        config.packer_release = 'v0.20'
        context = {'node_ip': '192.0.2.1'}

        with patch('actions.file.run_ssh') as mock_ssh:
            mock_ssh.side_effect = [
                (0, '', ''),      # mkdir
                (1, '', 'curl: (22) 404'),  # direct download fails
                # _get_split_parts returns parts
                (0, 'pve-9.qcow2.partaa\npve-9.qcow2.partab\n', ''),
                (0, '', ''),      # download partaa succeeds
                (1, '', 'curl: (22) 404'),  # download partab fails
                (0, '', ''),      # cleanup (rm parts)
            ]
            result = action.run(config, context)

        assert result.success is False
        assert 'partab' in result.message or 'Failed' in result.message

    def test_no_split_parts_returns_original_error(self):
        """If no split parts found, should return original download error."""
        from actions.file import DownloadGitHubReleaseAction

        action = DownloadGitHubReleaseAction(
            name='test',
            asset_name='nonexistent.qcow2',
            host_key='node_ip'
        )

        config = MagicMock()
        config.packer_release_repo = 'homestak-iac/packer'
        config.packer_release = 'v0.20'
        context = {'node_ip': '192.0.2.1'}

        with patch('actions.file.run_ssh') as mock_ssh:
            mock_ssh.side_effect = [
                (0, '', ''),      # mkdir
                (1, '', 'curl: (22) 404'),  # direct download fails
                (0, '', ''),      # _get_split_parts returns empty
            ]
            result = action.run(config, context)

        assert result.success is False
        assert 'no split parts found' in result.message

    def test_latest_tag_used_directly(self):
        """Should use 'latest' tag as-is in download URL (no API resolution)."""
        from actions.file import DownloadGitHubReleaseAction

        action = DownloadGitHubReleaseAction(
            name='test',
            asset_name='test.qcow2',
            host_key='node_ip'
        )

        config = MagicMock()
        config.packer_release_repo = 'homestak-iac/packer'
        config.packer_release = 'latest'
        context = {'node_ip': '192.0.2.1'}

        with patch('actions.file.run_ssh') as mock_ssh:
            mock_ssh.side_effect = [
                (0, '', ''),      # mkdir
                (0, '', ''),      # curl download
                (0, '', ''),      # mv rename (rename_ext='.img' by default)
                (0, '-rw-r--r-- 1 root root 123456 file', ''),  # ls verify
            ]
            result = action.run(config, context)

            # Verify download URL uses literal 'latest' tag
            download_call = mock_ssh.call_args_list[1]
            assert 'releases/download/latest/test.qcow2' in download_call[0][1]

        assert result.success is True

    def test_rename_extension(self):
        """Should rename .qcow2 to .img by default."""
        from actions.file import DownloadGitHubReleaseAction

        action = DownloadGitHubReleaseAction(
            name='test',
            asset_name='debian-12.qcow2',
            host_key='node_ip',
            rename_ext='.img'
        )

        config = MagicMock()
        config.packer_release_repo = 'homestak-iac/packer'
        config.packer_release = 'v0.20'
        context = {'node_ip': '192.0.2.1'}

        with patch('actions.file.run_ssh') as mock_ssh:
            mock_ssh.side_effect = [
                (0, '', ''),      # mkdir
                (0, '', ''),      # curl download
                (0, '', ''),      # mv rename
                (0, '-rw-r--r-- 1 root root 123456 file', ''),  # ls verify
            ]
            result = action.run(config, context)

        assert result.success is True
        assert 'debian-12.img' in result.message

    def test_get_split_parts_returns_sorted_list(self):
        """_get_split_parts should return sorted list of part filenames."""
        from actions.file import DownloadGitHubReleaseAction

        action = DownloadGitHubReleaseAction(
            name='test',
            asset_name='large-file.qcow2',
            host_key='node_ip'
        )

        with patch('actions.file.run_ssh') as mock_ssh:
            # API returns parts in arbitrary order
            mock_ssh.return_value = (0, 'large-file.qcow2.partab\nlarge-file.qcow2.partaa\n', '')
            parts = action._get_split_parts('repo/name', 'v1.0', '192.0.2.1', 'root')

        # Should be sorted alphabetically
        assert parts == ['large-file.qcow2.partab', 'large-file.qcow2.partaa']
