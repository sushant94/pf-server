"""Integration tests for _handle_file_changes using real Docker container."""

import base64

import docker
import pytest

from pf_server.models import FileChange, FileChangeType
from pf_server.user_context import UserContext, set_current_user, _current_user
from pf_server.ws_proxy import _handle_file_changes


DOCKER_BASE_CWD = "/workdir"


@pytest.fixture(scope="module")
def test_container():
    """Create a real Docker container for testing."""
    client = docker.from_env()
    container = client.containers.run(
        "pf-user-container:latest",
        command="sleep infinity",  # Keep container running
        detach=True,
        remove=True,  # Auto-remove when stopped
    )
    # Create workdir
    container.exec_run(f"mkdir -p {DOCKER_BASE_CWD}")
    yield container
    container.stop()


@pytest.fixture(autouse=True)
def setup_user_context():
    """Set up a test user context for all tests in this module."""
    user = UserContext(user_id="test-user", login="testuser")
    set_current_user(user)
    yield
    _current_user.set(None)


class TestHandleFileChanges:
    """Integration tests for _handle_file_changes with real Docker."""

    def test_add_file_utf8(self, test_container):
        """ADD creates file with correct content."""
        content = "print('hello world')"
        change = FileChange(
            path="test_utf8.py",
            type=FileChangeType.ADD,
            content_base64=base64.b64encode(content.encode()).decode(),
            encoding="utf8",
            timestamp="2025-01-07T00:00:00Z",
        )

        result = _handle_file_changes(change, test_container)

        assert result is True
        # Verify file was created with correct content
        check = test_container.exec_run(f"cat {DOCKER_BASE_CWD}/{change.path}")
        assert check.exit_code == 0
        assert content in check.output.decode()

    def test_add_file_base64_binary(self, test_container):
        """ADD with base64 encoding creates binary file correctly."""
        # Some binary data
        binary_data = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A])
        change = FileChange(
            path="test_binary.bin",
            type=FileChangeType.ADD,
            content_base64=base64.b64encode(binary_data).decode(),
            encoding="base64",
            timestamp="2025-01-07T00:00:00Z",
        )

        result = _handle_file_changes(change, test_container)

        assert result is True
        # Verify file exists
        check = test_container.exec_run(
            ["sh", "-c", f"test -f {DOCKER_BASE_CWD}/{change.path} && echo exists"]
        )
        assert b"exists" in check.output

    def test_delete_file(self, test_container):
        """DELETE removes file from container."""
        # First create a file to delete
        test_container.exec_run(f"touch {DOCKER_BASE_CWD}/to_delete.txt")

        change = FileChange(
            path="to_delete.txt",
            type=FileChangeType.DELETE,
            timestamp="2025-01-07T00:00:00Z",
        )

        result = _handle_file_changes(change, test_container)

        assert result is True
        # Verify file was deleted
        check = test_container.exec_run(f"test -f {DOCKER_BASE_CWD}/{change.path}")
        assert check.exit_code != 0  # File should not exist

    def test_modify_binary_replaces_content(self, test_container):
        """MODIFY binary replaces file content."""
        # Create initial file
        test_container.exec_run(f"echo 'original' > {DOCKER_BASE_CWD}/modify_test.bin")

        new_content = b"new binary content"
        change = FileChange(
            path="modify_test.bin",
            type=FileChangeType.MODIFY,
            content_base64=base64.b64encode(new_content).decode(),
            is_binary=True,
            timestamp="2025-01-07T00:00:00Z",
        )

        result = _handle_file_changes(change, test_container)

        assert result is True
        # Verify content was replaced
        check = test_container.exec_run(f"cat {DOCKER_BASE_CWD}/{change.path}")
        assert new_content in check.output

    def test_delete_nonexistent_succeeds(self, test_container):
        """DELETE on non-existent file still succeeds (rm -f behavior)."""
        change = FileChange(
            path="does_not_exist_12345.txt",
            type=FileChangeType.DELETE,
            timestamp="2025-01-07T00:00:00Z",
        )

        result = _handle_file_changes(change, test_container)

        # rm -f doesn't fail on missing files
        assert result is True
