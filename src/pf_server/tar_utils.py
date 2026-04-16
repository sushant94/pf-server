"""Utilities for extracting tar archives to Docker containers"""

import base64
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Tuple

from docker.models.containers import Container

from .config import settings
from .logging_config import get_logger

logger = get_logger(__name__)


def extract_tar_in_docker(
    archive_base64: str, container: Container, target_path: str = "/workspace"
) -> Tuple[bool, list[str], str]:
    """
    Extract base64 tar.gz archive into Docker container using docker cp + exec.

    This method works with any Docker container without requiring volume access.

    Args:
        archive_base64: Base64-encoded tar.gz archive
        container: Docker container object
        target_path: Target path inside container (default: /workspace)

    Returns:
        Tuple of (success, extracted_files, error_message)
    """
    # pf:ensures:extract_tar_in_docker.temp_cleanup temp file on host is always deleted even on exception
    # pf:ensures:extract_tar_in_docker.container_cleanup tar file in container is cleaned up on success
    # pf:ensures:extract_tar_in_docker.returns_tuple returns (bool, list, str) tuple with consistent semantics
    tmp_path = None
    start_time = time.perf_counter()

    try:
        # Decode base64 to temporary file
        tar_bytes = base64.b64decode(archive_base64)
        logger.info("tar_received", size_bytes=len(tar_bytes), mode="docker")

        # Write to temp file
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp.write(tar_bytes)
            tmp.flush()
            tmp_path = tmp.name

        # Copy tar into container
        container_tar_path = "/tmp/pf-sync.tar.gz"
        logger.debug("tar_copying_to_container", container_name=container.name)

        cp_cmd = ["docker", "cp", tmp_path, f"{container.name}:{container_tar_path}"]
        _cp_result = subprocess.run(cp_cmd, check=True, capture_output=True, text=True)

        # Create target directory if it doesn't exist
        mkdir_result = container.exec_run(
            cmd=["mkdir", "-p", target_path], workdir=str(settings.docker_base_cwd)
        )
        if mkdir_result.exit_code != 0:
            logger.debug(
                "tar_mkdir_warning",
                output=mkdir_result.output.decode() if mkdir_result.output else None,
            )

        # Extract tar inside container with verbose output
        extract_cmd = [
            "tar",
            "-xzf",
            container_tar_path,
            "-C",
            target_path,
            "--verbose",
        ]

        logger.debug("tar_extracting", mode="docker", target_path=target_path)
        extract_result = container.exec_run(
            cmd=extract_cmd, workdir=str(settings.docker_base_cwd)
        )

        if extract_result.exit_code != 0:
            error_msg = (
                extract_result.output.decode()
                if extract_result.output
                else "Unknown error"
            )
            logger.error("tar_extraction_failed", mode="docker", error=error_msg)
            return False, [], error_msg

        # Parse extracted files from tar verbose output
        output = extract_result.output.decode() if extract_result.output else ""
        files = [line.strip() for line in output.split("\n") if line.strip()]

        duration_ms = int((time.perf_counter() - start_time) * 1000)
        logger.info(
            "tar_extracted",
            mode="docker",
            file_count=len(files),
            duration_ms=duration_ms,
        )

        # Cleanup tar file in container
        cleanup_result = container.exec_run(cmd=["rm", "-f", container_tar_path])
        if cleanup_result.exit_code != 0:
            logger.debug("tar_cleanup_warning", location="container")

        return True, files, ""

    except subprocess.CalledProcessError as e:
        error_msg = e.stderr if e.stderr else str(e)
        logger.error("tar_docker_command_failed", error=error_msg)
        return False, [], error_msg
    except Exception as e:
        logger.error("tar_extraction_error", error=str(e))
        return False, [], str(e)
    finally:
        # Cleanup temp file on host
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception as e:
                logger.debug("tar_temp_cleanup_failed", path=tmp_path, error=str(e))


def extract_tar_to_mounted_volume(
    archive_base64: str, mount_path: Path, target_subdir: str = ""
) -> Tuple[bool, list[str], str]:
    """
    Extract base64 tar.gz archive directly to mounted volume on host filesystem.

    This method is faster as it bypasses Docker API, but requires the container
    to use bind mounts or accessible volumes.

    Args:
        archive_base64: Base64-encoded tar.gz archive
        mount_path: Path to mounted volume on host (e.g., /var/lib/docker/volumes/...)
        target_subdir: Subdirectory within mount to extract to

    Returns:
        Tuple of (success, extracted_files, error_message)
    """
    # pf:ensures:extract_tar_to_mounted_volume.temp_cleanup temp file is always deleted even on exception
    # pf:ensures:extract_tar_to_mounted_volume.creates_target target directory is created if it doesn't exist
    tmp_path = None
    start_time = time.perf_counter()

    try:
        # Decode base64 to temporary file
        tar_bytes = base64.b64decode(archive_base64)
        logger.info("tar_received", size_bytes=len(tar_bytes), mode="mounted")

        # Write to temp file
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp.write(tar_bytes)
            tmp.flush()
            tmp_path = tmp.name

        # Determine extraction target
        extract_target = mount_path / target_subdir if target_subdir else mount_path
        extract_target.mkdir(parents=True, exist_ok=True)

        logger.debug("tar_extracting", mode="mounted", target_path=str(extract_target))

        # Extract using host tar command
        extract_cmd = ["tar", "-xzf", tmp_path, "-C", str(extract_target), "--verbose"]

        result = subprocess.run(extract_cmd, check=True, capture_output=True, text=True)

        # Parse extracted files from tar verbose output
        files = [line.strip() for line in result.stdout.split("\n") if line.strip()]

        duration_ms = int((time.perf_counter() - start_time) * 1000)
        logger.info(
            "tar_extracted",
            mode="mounted",
            file_count=len(files),
            duration_ms=duration_ms,
        )

        return True, files, ""

    except subprocess.CalledProcessError as e:
        error_msg = e.stderr if e.stderr else str(e)
        logger.error("tar_extraction_failed", mode="mounted", error=error_msg)
        return False, [], error_msg
    except Exception as e:
        logger.error("tar_extraction_error", error=str(e))
        return False, [], str(e)
    finally:
        # Cleanup temp file
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception as e:
                logger.debug("tar_temp_cleanup_failed", path=tmp_path, error=str(e))


def validate_archive_security(
    archive_base64: str, max_compressed_mb: int = 100
) -> Tuple[bool, str]:
    """
    Validate archive for security concerns before extraction.

    Args:
        archive_base64: Base64-encoded tar.gz archive
        max_compressed_mb: Maximum compressed size in MB

    Returns:
        Tuple of (is_valid, error_message)
    """
    # pf:requires:validate_archive_security.valid_base64 archive_base64 must be valid base64
    # pf:ensures:validate_archive_security.rejects_oversized returns (False, error) if size exceeds max_compressed_mb
    # pf:ensures:validate_archive_security.empty_error_on_valid returns (True, "") if valid
    try:
        # Check compressed size
        tar_bytes = base64.b64decode(archive_base64)
        compressed_mb = len(tar_bytes) / (1024 * 1024)

        if compressed_mb > max_compressed_mb:
            return (
                False,
                f"Archive too large: {compressed_mb:.1f}MB (max: {max_compressed_mb}MB)",
            )

        # TODO: Additional security checks:
        # - List tar contents and check for path traversal (../)
        # - Check for symlinks pointing outside workspace
        # - Validate uncompressed size ratio (zip bomb detection)

        return True, ""

    except Exception as e:
        return False, f"Archive validation failed: {e}"
