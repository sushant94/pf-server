"""Unit tests for tar_utils module.

Tests cover:
- Archive security validation (size limits, malformed archives)
- These are unit tests that don't require Docker
"""

import base64
import io
import tarfile

import pytest


class TestValidateArchiveSecurity:
    """Tests for validate_archive_security function."""

    def test_valid_small_archive(self):
        """Valid small archive passes validation."""
        from pf_server.tar_utils import validate_archive_security

        # Create a valid small tar.gz archive
        archive = self._create_test_archive({"test.txt": b"Hello, World!"})
        is_valid, error = validate_archive_security(archive, max_compressed_mb=100)

        assert is_valid is True
        assert error == ""

    def test_archive_exceeds_max_size(self):
        """Archive exceeding max size fails validation."""
        from pf_server.tar_utils import validate_archive_security

        # Create a large-ish archive (this is just over 1MB compressed)
        large_content = b"x" * (1024 * 1024)  # 1MB of content
        archive = self._create_test_archive({"large.bin": large_content})

        # Set a very low limit
        is_valid, error = validate_archive_security(archive, max_compressed_mb=0.0001)

        assert is_valid is False
        assert "Archive too large" in error
        assert "max:" in error

    def test_archive_exactly_at_limit(self):
        """Archive at exact size limit passes validation."""
        from pf_server.tar_utils import validate_archive_security

        # Create a small archive
        archive = self._create_test_archive({"small.txt": b"tiny"})
        archive_bytes = base64.b64decode(archive)
        size_mb = len(archive_bytes) / (1024 * 1024)

        # Set limit to exactly the archive size (plus small buffer)
        is_valid, error = validate_archive_security(
            archive, max_compressed_mb=size_mb + 0.001
        )

        assert is_valid is True
        assert error == ""

    def test_invalid_base64_fails(self):
        """Invalid base64 string fails validation."""
        from pf_server.tar_utils import validate_archive_security

        is_valid, error = validate_archive_security(
            "not-valid-base64!!!", max_compressed_mb=100
        )

        assert is_valid is False
        assert "validation failed" in error.lower()

    def test_empty_archive_passes(self):
        """Empty but valid tar archive passes validation."""
        from pf_server.tar_utils import validate_archive_security

        # Create empty tar.gz
        archive = self._create_test_archive({})
        is_valid, error = validate_archive_security(archive, max_compressed_mb=100)

        assert is_valid is True
        assert error == ""

    def test_multiple_files_archive(self):
        """Archive with multiple files passes validation."""
        from pf_server.tar_utils import validate_archive_security

        files = {
            "dir/file1.txt": b"Content 1",
            "dir/file2.txt": b"Content 2",
            "dir/subdir/file3.py": b"print('hello')",
        }
        archive = self._create_test_archive(files)
        is_valid, error = validate_archive_security(archive, max_compressed_mb=100)

        assert is_valid is True
        assert error == ""

    def test_default_max_size(self):
        """Default max size parameter works correctly."""
        from pf_server.tar_utils import validate_archive_security

        archive = self._create_test_archive({"test.txt": b"data"})
        # Should use default of 100MB
        is_valid, error = validate_archive_security(archive)

        assert is_valid is True
        assert error == ""

    def test_zero_max_size_rejects_all(self):
        """Zero max size rejects any archive."""
        from pf_server.tar_utils import validate_archive_security

        # Even tiny archive should be rejected with 0 max size
        archive = self._create_test_archive({"tiny.txt": b"x"})
        is_valid, error = validate_archive_security(archive, max_compressed_mb=0)

        assert is_valid is False
        assert "too large" in error.lower()

    @staticmethod
    def _create_test_archive(files: dict[str, bytes]) -> str:
        """Create a base64-encoded tar.gz archive from a dict of filename -> content."""
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode="w:gz") as tar:
            for filename, content in files.items():
                info = tarfile.TarInfo(name=filename)
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))

        tar_buffer.seek(0)
        return base64.b64encode(tar_buffer.read()).decode("ascii")


class TestConfigFieldValidation:
    """Tests for config field validators."""

    def test_jwt_secret_minimum_length(self):
        """JWT secret must be at least 32 characters."""
        from pydantic import ValidationError

        from pf_server.config import Settings

        with pytest.raises(ValidationError) as exc_info:
            Settings(
                github_client_id="test",
                github_client_secret="test",
                jwt_secret="short",  # Too short
            )

        assert "32 characters" in str(exc_info.value)

    def test_jwt_secret_valid_length(self):
        """JWT secret of 32+ characters passes validation."""
        from pf_server.config import Settings

        settings = Settings(
            github_client_id="test",
            github_client_secret="test",
            jwt_secret="a" * 32,  # Exactly 32 characters
        )
        assert len(settings.jwt_secret) == 32

    def test_tar_extraction_mode_literal(self):
        """Tar extraction mode must be 'docker' or 'mounted'."""
        from pydantic import ValidationError

        from pf_server.config import Settings

        with pytest.raises(ValidationError):
            Settings(
                github_client_id="test",
                github_client_secret="test",
                jwt_secret="a" * 32,
                tar_extraction_mode="invalid",  # Not allowed
            )

    def test_tar_extraction_mode_valid_values(self):
        """Valid tar extraction modes are accepted."""
        from pf_server.config import Settings

        for mode in ["docker", "mounted"]:
            settings = Settings(
                github_client_id="test",
                github_client_secret="test",
                jwt_secret="a" * 32,
                tar_extraction_mode=mode,
            )
            assert settings.tar_extraction_mode == mode

    def test_max_tar_size_must_be_positive(self):
        """Max tar size must be positive."""
        from pydantic import ValidationError

        from pf_server.config import Settings

        with pytest.raises(ValidationError):
            Settings(
                github_client_id="test",
                github_client_secret="test",
                jwt_secret="a" * 32,
                max_tar_size_mb=0,  # Must be > 0
            )

        with pytest.raises(ValidationError):
            Settings(
                github_client_id="test",
                github_client_secret="test",
                jwt_secret="a" * 32,
                max_tar_size_mb=-1,  # Must be > 0
            )

    def test_debounce_must_be_positive(self):
        """Analysis debounce must be positive."""
        from pydantic import ValidationError

        from pf_server.config import Settings

        with pytest.raises(ValidationError):
            Settings(
                github_client_id="test",
                github_client_secret="test",
                jwt_secret="a" * 32,
                lite_analysis_debounce_ms=0,  # Must be > 0
            )
