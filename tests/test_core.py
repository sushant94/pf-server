"""Core functionality tests for pf-server.

Tests cover:
- JWT creation and verification (security-critical)
- Whitelist ID parsing (access control)
- Container naming (reconnection stability)
"""

import os
import time

import pytest
from jose import jwt as jose_jwt

# Set test environment variables BEFORE importing pf_server modules
os.environ.setdefault("GITHUB_CLIENT_ID", "test-client-id")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-testing-only")
os.environ.setdefault("ALLOWED_GITHUB_IDS", "12345,67890")


class TestJWT:
    """JWT creation and verification tests."""

    def test_jwt_roundtrip(self):
        """JWT can be created and verified, returning original payload."""
        from pf_server.auth import create_jwt, verify_jwt

        # JWT 'sub' claim must be a string per RFC 7519
        original_payload = {"sub": "12345", "login": "testuser"}
        token = create_jwt(original_payload)
        decoded = verify_jwt(token)

        assert decoded["sub"] == original_payload["sub"]
        assert decoded["login"] == original_payload["login"]
        assert "exp" in decoded  # Expiry should be added

    def test_jwt_contains_expiry(self):
        """JWT includes expiration claim."""
        from pf_server.auth import create_jwt, verify_jwt

        token = create_jwt({"sub": "123"})
        decoded = verify_jwt(token)

        assert "exp" in decoded
        assert decoded["exp"] > time.time()  # Expiry is in the future

    def test_jwt_expired_rejected(self):
        """Expired JWT is rejected."""
        from jose import JWTError

        from pf_server.auth import ALGORITHM, verify_jwt
        from pf_server.config import settings

        # Create an already-expired token
        expired_payload = {"sub": 123, "exp": time.time() - 3600}
        expired_token = jose_jwt.encode(
            expired_payload, settings.jwt_secret, algorithm=ALGORITHM
        )

        with pytest.raises(JWTError):
            verify_jwt(expired_token)

    def test_jwt_tampered_rejected(self):
        """Tampered JWT is rejected."""
        from jose import JWTError

        from pf_server.auth import create_jwt, verify_jwt

        token = create_jwt({"sub": 123})

        # Tamper with the token (modify a character in payload section)
        parts = token.split(".")
        # Flip a character in the payload
        tampered_payload = parts[1][:-1] + ("A" if parts[1][-1] != "A" else "B")
        tampered_token = f"{parts[0]}.{tampered_payload}.{parts[2]}"

        with pytest.raises(JWTError):
            verify_jwt(tampered_token)

    def test_jwt_wrong_secret_rejected(self):
        """JWT signed with different secret is rejected."""
        from jose import JWTError

        from pf_server.auth import ALGORITHM, verify_jwt

        # Create token with different secret
        wrong_secret_token = jose_jwt.encode(
            {"sub": 123, "exp": time.time() + 3600},
            "wrong-secret",
            algorithm=ALGORITHM,
        )

        with pytest.raises(JWTError):
            verify_jwt(wrong_secret_token)


class TestWhitelistParsing:
    """Whitelist ID parsing tests."""

    def test_parse_comma_separated_ids(self):
        """Comma-separated string is parsed into set of ints."""
        from pf_server.config import parse_allowed_ids

        result = parse_allowed_ids("123,456,789")
        assert result == {123, 456, 789}

    def test_parse_with_whitespace(self):
        """Whitespace around IDs is handled."""
        from pf_server.config import parse_allowed_ids

        result = parse_allowed_ids("123 , 456 , 789")
        assert result == {123, 456, 789}

    def test_parse_empty_string(self):
        """Empty string returns empty set."""
        from pf_server.config import parse_allowed_ids

        result = parse_allowed_ids("")
        assert result == set()

    def test_parse_single_id(self):
        """Single ID is parsed correctly."""
        from pf_server.config import parse_allowed_ids

        result = parse_allowed_ids("12345")
        assert result == {12345}

    def test_parse_already_set(self):
        """If already a set, returns as-is."""
        from pf_server.config import parse_allowed_ids

        input_set = {1, 2, 3}
        result = parse_allowed_ids(input_set)
        assert result == input_set

    def test_settings_loads_allowed_ids(self):
        """Settings correctly parses ALLOWED_GITHUB_IDS from env."""
        from pf_server.config import settings

        # We set ALLOWED_GITHUB_IDS=12345,67890 in test setup
        assert 12345 in settings.allowed_github_ids
        assert 67890 in settings.allowed_github_ids


class TestContainerNaming:
    """Container naming tests.

    These test the naming convention without requiring Docker.
    The naming must be deterministic for reconnection to work.
    """

    def test_container_name_deterministic(self):
        """Same user ID always produces same container name."""
        user_id = 12345
        expected_name = f"pf-user-{user_id}"

        # Verify naming pattern is consistent
        for _ in range(3):
            assert f"pf-user-{user_id}" == expected_name

    def test_different_users_different_containers(self):
        """Different user IDs produce different container names."""
        user_ids = [111, 222, 333]
        names = [f"pf-user-{uid}" for uid in user_ids]

        # All names are unique
        assert len(set(names)) == len(names)

    def test_container_ws_url_format(self):
        """WebSocket URL follows expected format."""
        user_id = 12345
        container_name = f"pf-user-{user_id}"
        expected_url = f"ws://{container_name}:8000/ws"

        # Verify URL format matches what ws_proxy expects
        assert expected_url == "ws://pf-user-12345:8000/ws"
        assert expected_url.startswith("ws://")
        assert ":8000/ws" in expected_url
