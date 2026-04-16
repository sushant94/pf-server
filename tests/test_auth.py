"""Tests for authentication boundary.

These tests verify the contract of the auth functions:
- Valid token → UserContext returned AND context set
- Invalid token → None returned AND context NOT set
"""

import pytest

from pf_server.auth import create_jwt
from pf_server.main import authenticate
from pf_server.user_context import get_current_user, _current_user


class TestAuthenticate:
    """Tests for the authenticate() function - the auth boundary."""

    def setup_method(self):
        """Reset context before each test."""
        _current_user.set(None)

    def teardown_method(self):
        """Reset context after each test."""
        _current_user.set(None)

    def test_valid_token_returns_user_context(self):
        """Valid JWT returns UserContext with correct user_id and login."""
        token = create_jwt({"sub": "12345", "login": "testuser"})

        result = authenticate(token)

        assert result is not None
        assert result.user_id == "12345"
        assert result.login == "testuser"

    def test_valid_token_sets_context(self):
        """Valid JWT sets user context accessible via get_current_user()."""
        token = create_jwt({"sub": "12345", "login": "testuser"})

        authenticate(token)

        user = get_current_user()
        assert user.user_id == "12345"

    def test_invalid_token_returns_none(self):
        """Invalid JWT returns None."""
        result = authenticate("invalid.token.here")

        assert result is None

    def test_invalid_token_does_not_set_context(self):
        """Invalid JWT does not set user context."""
        authenticate("invalid.token.here")

        with pytest.raises(RuntimeError, match="No user context"):
            get_current_user()

    def test_expired_token_returns_none(self):
        """Expired JWT returns None."""
        from jose import jwt
        from pf_server.config import settings
        import time

        # Create token that expired 1 hour ago
        expired_payload = {"sub": "123", "login": "test", "exp": time.time() - 3600}
        expired_token = jwt.encode(
            expired_payload, settings.jwt_secret, algorithm="HS256"
        )

        result = authenticate(expired_token)

        assert result is None

    def test_tampered_token_returns_none(self):
        """Tampered JWT returns None."""
        token = create_jwt({"sub": "12345"})
        # Tamper with the token
        tampered = token[:-5] + "XXXXX"

        result = authenticate(tampered)

        assert result is None

    def test_token_without_sub_claim_returns_none(self):
        """JWT missing 'sub' claim fails gracefully."""
        from jose import jwt
        from pf_server.config import settings
        import time

        # Token with no sub claim
        payload = {"login": "test", "exp": time.time() + 3600}
        token = jwt.encode(payload, settings.jwt_secret, algorithm="HS256")

        # Should fail because payload["sub"] will raise KeyError
        result = authenticate(token)

        assert result is None


class TestPFTokenVerifier:
    """Tests for MCP's token verifier - same contract as authenticate()."""

    def setup_method(self):
        _current_user.set(None)

    def teardown_method(self):
        _current_user.set(None)

    @pytest.mark.asyncio
    async def test_valid_token_returns_access_token(self):
        """Valid JWT returns AccessToken with client_id set to user_id."""
        from pf_mcp.auth import PFTokenVerifier

        token = create_jwt({"sub": "67890", "login": "alice"})
        verifier = PFTokenVerifier()

        result = await verifier.verify_token(token)

        assert result is not None
        assert result.client_id == "67890"
        assert result.token == token

    @pytest.mark.asyncio
    async def test_valid_token_sets_context(self):
        """Valid JWT sets user context."""
        from pf_mcp.auth import PFTokenVerifier

        token = create_jwt({"sub": "67890", "login": "alice"})
        verifier = PFTokenVerifier()

        await verifier.verify_token(token)

        user = get_current_user()
        assert user.user_id == "67890"
        assert user.login == "alice"

    @pytest.mark.asyncio
    async def test_invalid_token_returns_none(self):
        """Invalid JWT returns None."""
        from pf_mcp.auth import PFTokenVerifier

        verifier = PFTokenVerifier()

        result = await verifier.verify_token("garbage")

        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_token_does_not_set_context(self):
        """Invalid JWT does not set user context."""
        from pf_mcp.auth import PFTokenVerifier

        verifier = PFTokenVerifier()
        await verifier.verify_token("garbage")

        with pytest.raises(RuntimeError):
            get_current_user()
