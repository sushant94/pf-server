"""JWT token verification for MCP server.

Wraps pf-server's existing JWT validation and sets the UserContext
upon successful authentication.
"""
# pf:invariant:auth_module.atomic_side_effect UserContext set atomically with AccessToken return

from jose import JWTError

from fastmcp.server.auth.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_http_headers

from pf_server.auth import verify_jwt
from pf_server.logging_config import bind_request_context, get_logger
from pf_server.user_context import UserContext, set_current_user

logger = get_logger(__name__)


class PFTokenVerifier(TokenVerifier):
    """Token verifier that validates JWT tokens using pf-server's auth.

    Upon successful verification, automatically populates the UserContext
    so it's available throughout the request lifecycle.
    """

    # pf:invariant:PFTokenVerifier.context_iff_valid user context set iff token valid
    async def verify_token(self, token: str) -> AccessToken | None:
        # pf:requires:verify_token.token_nonempty token must be non-empty string
        # pf:ensures:verify_token.valid_returns_access_token valid JWT returns AccessToken with client_id=user_id
        # pf:ensures:verify_token.invalid_returns_none invalid/expired/tampered JWT returns None
        # pf:ensures:verify_token.valid_sets_context valid token sets user context via set_current_user
        # pf:ensures:verify_token.invalid_no_context invalid token does not modify user context
        """Verify JWT token and set user context.

        Args:
            token: The JWT token string (without "Bearer " prefix).

        Returns:
            AccessToken if valid, None if invalid.
        """
        try:
            payload = verify_jwt(token)
            user_id = payload["sub"]
            login = payload.get("login")

            # Get project name from header
            headers = get_http_headers()
            project_name = headers.get("x-project-name")

            # Set user context immediately upon successful auth
            user = UserContext(user_id=user_id, login=login)
            if project_name:
                user.set_project_name(project_name)
            set_current_user(user)
            bind_request_context(user_id=user_id, login=login)
            return AccessToken(
                token=token,
                client_id=user_id,
                scopes=[],
            )
        except JWTError as e:
            # pf:assert:verify_token.jwt_error_logged JWTError always logged before returning None
            logger.debug("mcp_jwt_verification_failed", error=str(e))
            return None
        except KeyError as e:
            # pf:assert:verify_token.missing_sub_logged missing 'sub' claim logged as warning
            logger.warning("mcp_jwt_payload_missing_field", field=str(e))
            return None
