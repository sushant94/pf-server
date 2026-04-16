"""Tests for OpenCode REST API client.

Tests cover:
- Health check
- MCP configuration (caching)
- Session management
- Prompt submission
- Event streaming
- Cancellation
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


class TestOpenCodeClientHealthCheck:
    """Tests for OpenCodeClient.health_check."""

    @pytest.mark.asyncio
    async def test_health_check_returns_true_when_healthy(self):
        """health_check returns True when server responds healthy."""
        from pf_server.opencode_client import OpenCodeClient

        client = OpenCodeClient("http://localhost:5000")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"healthy": True, "version": "1.0.0"}

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http_client = AsyncMock()
            mock_http_client.get = AsyncMock(return_value=mock_response)
            mock_ensure.return_value = mock_http_client

            result = await client.health_check()

        assert result is True

    @pytest.mark.asyncio
    async def test_health_check_returns_false_when_unhealthy(self):
        """health_check returns False when server responds unhealthy."""
        from pf_server.opencode_client import OpenCodeClient

        client = OpenCodeClient("http://localhost:5000")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"healthy": False}

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http_client = AsyncMock()
            mock_http_client.get = AsyncMock(return_value=mock_response)
            mock_ensure.return_value = mock_http_client

            result = await client.health_check()

        assert result is False

    @pytest.mark.asyncio
    async def test_health_check_returns_false_on_error(self):
        """health_check returns False when request fails."""
        from pf_server.opencode_client import OpenCodeClient

        client = OpenCodeClient("http://localhost:5000")

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http_client = AsyncMock()
            mock_http_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
            mock_ensure.return_value = mock_http_client

            result = await client.health_check()

        assert result is False


class TestOpenCodeClientMCPConfig:
    """Tests for OpenCodeClient.ensure_mcp."""

    @pytest.mark.asyncio
    async def test_ensure_mcp_sends_config(self):
        """ensure_mcp sends MCP configuration to server."""
        from pf_server.opencode_client import MCPConfig, OpenCodeClient

        client = OpenCodeClient("http://localhost:5000")

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http_client = AsyncMock()
            mock_http_client.post = AsyncMock(return_value=mock_response)
            mock_ensure.return_value = mock_http_client

            config = MCPConfig(command=["test-server", "--port", "8080"])
            await client.ensure_mcp("/workdir/project", "test-mcp", config)

        mock_http_client.post.assert_called_once()
        call_args = mock_http_client.post.call_args
        assert call_args[0][0] == "http://localhost:5000/config"
        assert "x-opencode-directory" in call_args[1]["headers"]

    @pytest.mark.asyncio
    async def test_ensure_mcp_caches_config(self):
        """ensure_mcp only sends config once per name/project combination."""
        from pf_server.opencode_client import MCPConfig, OpenCodeClient

        client = OpenCodeClient("http://localhost:5000")

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http_client = AsyncMock()
            mock_http_client.post = AsyncMock(return_value=mock_response)
            mock_ensure.return_value = mock_http_client

            config = MCPConfig(command=["test-server"])

            # Call twice with same name/project
            await client.ensure_mcp("/workdir/project", "test-mcp", config)
            await client.ensure_mcp("/workdir/project", "test-mcp", config)

        # Should only be called once
        assert mock_http_client.post.call_count == 1

    @pytest.mark.asyncio
    async def test_ensure_mcp_different_projects_not_cached(self):
        """ensure_mcp sends separate configs for different projects."""
        from pf_server.opencode_client import MCPConfig, OpenCodeClient

        client = OpenCodeClient("http://localhost:5000")

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http_client = AsyncMock()
            mock_http_client.post = AsyncMock(return_value=mock_response)
            mock_ensure.return_value = mock_http_client

            config = MCPConfig(command=["test-server"])

            # Call with different projects
            await client.ensure_mcp("/workdir/project-a", "test-mcp", config)
            await client.ensure_mcp("/workdir/project-b", "test-mcp", config)

        # Should be called twice (different projects)
        assert mock_http_client.post.call_count == 2


class TestOpenCodeClientSessionManagement:
    """Tests for session management methods."""

    @pytest.mark.asyncio
    async def test_get_latest_session_returns_root_session(self):
        """get_latest_session returns first root session."""
        from pf_server.opencode_client import OpenCodeClient

        client = OpenCodeClient("http://localhost:5000")

        sessions = [
            {"id": "ses_root", "parentID": None},
            {"id": "ses_child", "parentID": "ses_root"},
        ]
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = sessions

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http_client = AsyncMock()
            mock_http_client.get = AsyncMock(return_value=mock_response)
            mock_ensure.return_value = mock_http_client

            result = await client.get_latest_session("/workdir/project")

        assert result == "ses_root"

    @pytest.mark.asyncio
    async def test_get_latest_session_returns_none_when_no_root(self):
        """get_latest_session returns None when no root session exists."""
        from pf_server.opencode_client import OpenCodeClient

        client = OpenCodeClient("http://localhost:5000")

        sessions = [
            {"id": "ses_child1", "parentID": "ses_parent"},
            {"id": "ses_child2", "parentID": "ses_parent"},
        ]
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = sessions

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http_client = AsyncMock()
            mock_http_client.get = AsyncMock(return_value=mock_response)
            mock_ensure.return_value = mock_http_client

            result = await client.get_latest_session("/workdir/project")

        assert result is None

    @pytest.mark.asyncio
    async def test_create_session_returns_session_id(self):
        """create_session creates session and returns ID."""
        from pf_server.opencode_client import OpenCodeClient

        client = OpenCodeClient("http://localhost:5000")

        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"id": "ses_new123"}

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http_client = AsyncMock()
            mock_http_client.post = AsyncMock(return_value=mock_response)
            mock_ensure.return_value = mock_http_client

            result = await client.create_session("/workdir/project")

        assert result == "ses_new123"
        assert client._active_sessions["/workdir/project"] == "ses_new123"


class TestOpenCodeClientPrompt:
    """Tests for OpenCodeClient.prompt_async."""

    @pytest.mark.asyncio
    async def test_prompt_async_creates_session_when_none_exists(self):
        """prompt_async creates new session when none exists."""
        from pf_server.opencode_client import OpenCodeClient

        client = OpenCodeClient("http://localhost:5000")

        # Mock session creation
        create_response = MagicMock()
        create_response.status_code = 201
        create_response.json.return_value = {"id": "ses_new"}

        # Mock prompt submission
        prompt_response = MagicMock()
        prompt_response.status_code = 202

        # Mock get latest session returning None
        get_response = MagicMock()
        get_response.status_code = 200
        get_response.json.return_value = []

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http_client = AsyncMock()
            mock_http_client.get = AsyncMock(return_value=get_response)
            mock_http_client.post = AsyncMock(
                side_effect=[create_response, prompt_response]
            )
            mock_ensure.return_value = mock_http_client

            result = await client.prompt_async(
                project_path="/workdir/project",
                text="Analyze this",
                continue_session=True,
            )

        assert result == "ses_new"

    @pytest.mark.asyncio
    async def test_prompt_async_reuses_existing_session(self):
        """prompt_async reuses existing session when continue_session is True."""
        from pf_server.opencode_client import OpenCodeClient

        client = OpenCodeClient("http://localhost:5000")
        client._active_sessions["/workdir/project"] = "ses_existing"

        mock_response = MagicMock()
        mock_response.status_code = 202

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http_client = AsyncMock()
            mock_http_client.post = AsyncMock(return_value=mock_response)
            mock_ensure.return_value = mock_http_client

            result = await client.prompt_async(
                project_path="/workdir/project",
                text="Analyze this",
                continue_session=True,
            )

        assert result == "ses_existing"
        # Should have posted to existing session
        call_args = mock_http_client.post.call_args
        assert "/session/ses_existing/prompt" in call_args[0][0]


class TestOpenCodeClientAbort:
    """Tests for OpenCodeClient.abort."""

    @pytest.mark.asyncio
    async def test_abort_calls_abort_endpoint(self):
        """abort calls the session abort endpoint."""
        from pf_server.opencode_client import OpenCodeClient

        client = OpenCodeClient("http://localhost:5000")
        client._active_sessions["/workdir/project"] = "ses_active"

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http_client = AsyncMock()
            mock_http_client.post = AsyncMock(return_value=mock_response)
            mock_ensure.return_value = mock_http_client

            result = await client.abort("/workdir/project")

        assert result is True
        call_args = mock_http_client.post.call_args
        assert "/session/ses_active/abort" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_abort_returns_true_when_no_session(self):
        """abort returns True when no session exists."""
        from pf_server.opencode_client import OpenCodeClient

        client = OpenCodeClient("http://localhost:5000")

        # Mock get_latest_session returning None
        with patch.object(client, "get_latest_session", return_value=None):
            result = await client.abort("/workdir/project")

        assert result is True


class TestOpenCodeClientGetSession:
    """Tests for OpenCodeClient.get_session."""

    @pytest.mark.asyncio
    async def test_get_session_returns_session_info(self):
        """get_session returns session information."""
        from pf_server.opencode_client import OpenCodeClient

        client = OpenCodeClient("http://localhost:5000")

        session_data = {"id": "ses_123", "status": "idle", "agent": "plan"}
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = session_data

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http_client = AsyncMock()
            mock_http_client.get = AsyncMock(return_value=mock_response)
            mock_ensure.return_value = mock_http_client

            result = await client.get_session("ses_123", "/workdir/project")

        assert result == session_data
        call_args = mock_http_client.get.call_args
        assert "/session/ses_123" in call_args[0][0]
        assert "x-opencode-directory" in call_args[1]["headers"]

    @pytest.mark.asyncio
    async def test_get_session_raises_on_error(self):
        """get_session raises RuntimeError on failure."""
        from pf_server.opencode_client import OpenCodeClient

        client = OpenCodeClient("http://localhost:5000")

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Session not found"

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http_client = AsyncMock()
            mock_http_client.get = AsyncMock(return_value=mock_response)
            mock_ensure.return_value = mock_http_client

            with pytest.raises(RuntimeError, match="Failed to get session"):
                await client.get_session("nonexistent", "/workdir/project")


class TestOpenCodeClientGetMessages:
    """Tests for OpenCodeClient.get_messages."""

    @pytest.mark.asyncio
    async def test_get_messages_returns_messages(self):
        """get_messages returns list of messages."""
        from pf_server.opencode_client import OpenCodeClient

        client = OpenCodeClient("http://localhost:5000")

        messages = [
            {"id": "msg_1", "info": {"role": "user"}, "parts": []},
            {"id": "msg_2", "info": {"role": "assistant"}, "parts": []},
        ]
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = messages

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http_client = AsyncMock()
            mock_http_client.get = AsyncMock(return_value=mock_response)
            mock_ensure.return_value = mock_http_client

            result = await client.get_messages("ses_123", "/workdir/project")

        assert result == messages
        assert len(result) == 2
        call_args = mock_http_client.get.call_args
        assert "/session/ses_123/message" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_get_messages_raises_on_error(self):
        """get_messages raises RuntimeError on failure."""
        from pf_server.opencode_client import OpenCodeClient

        client = OpenCodeClient("http://localhost:5000")

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal server error"

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http_client = AsyncMock()
            mock_http_client.get = AsyncMock(return_value=mock_response)
            mock_ensure.return_value = mock_http_client

            with pytest.raises(RuntimeError, match="Failed to get messages"):
                await client.get_messages("ses_123", "/workdir/project")


class TestOpenCodeClientSendPrompt:
    """Tests for OpenCodeClient.send_prompt (blocking)."""

    @pytest.mark.asyncio
    async def test_send_prompt_returns_response(self):
        """send_prompt returns response dict."""
        from pf_server.opencode_client import OpenCodeClient

        client = OpenCodeClient("http://localhost:5000")

        response_data = {"stopReason": "end_turn", "messageId": "msg_123"}
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = response_data

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http_client = AsyncMock()
            mock_http_client.post = AsyncMock(return_value=mock_response)
            mock_ensure.return_value = mock_http_client

            result = await client.send_prompt(
                session_id="ses_123",
                project_path="/workdir/project",
                text="Create a plan",
                agent="plan",
            )

        assert result == response_data
        call_args = mock_http_client.post.call_args
        assert "/session/ses_123/message" in call_args[0][0]
        assert call_args[1]["json"]["agent"] == "plan"

    @pytest.mark.asyncio
    async def test_send_prompt_raises_on_error(self):
        """send_prompt raises RuntimeError on failure."""
        from pf_server.opencode_client import OpenCodeClient

        client = OpenCodeClient("http://localhost:5000")

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "Bad request"

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http_client = AsyncMock()
            mock_http_client.post = AsyncMock(return_value=mock_response)
            mock_ensure.return_value = mock_http_client

            with pytest.raises(RuntimeError, match="Failed to send prompt"):
                await client.send_prompt("ses_123", "/workdir", "test", "plan")


class TestOpenCodeClientQuestions:
    """Tests for question-related methods."""

    @pytest.mark.asyncio
    async def test_list_questions_returns_questions(self):
        """list_questions returns list of pending questions."""
        from pf_server.opencode_client import OpenCodeClient

        client = OpenCodeClient("http://localhost:5000")

        questions = [
            {"requestId": "req_1", "questions": [{"question": "Which approach?"}]},
        ]
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = questions

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http_client = AsyncMock()
            mock_http_client.get = AsyncMock(return_value=mock_response)
            mock_ensure.return_value = mock_http_client

            result = await client.list_questions("/workdir/project")

        assert result == questions
        call_args = mock_http_client.get.call_args
        assert "/question" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_reply_to_question_sends_reply(self):
        """reply_to_question sends answers to server."""
        from pf_server.opencode_client import OpenCodeClient

        client = OpenCodeClient("http://localhost:5000")

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http_client = AsyncMock()
            mock_http_client.post = AsyncMock(return_value=mock_response)
            mock_ensure.return_value = mock_http_client

            await client.reply_to_question(
                request_id="req_123",
                answers=[["Option A"]],
                project_path="/workdir/project",
            )

        call_args = mock_http_client.post.call_args
        assert "/question/req_123/reply" in call_args[0][0]
        assert call_args[1]["json"]["answers"] == [["Option A"]]

    @pytest.mark.asyncio
    async def test_reject_question_sends_rejection(self):
        """reject_question sends rejection to server."""
        from pf_server.opencode_client import OpenCodeClient

        client = OpenCodeClient("http://localhost:5000")

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(client, "_ensure_client") as mock_ensure:
            mock_http_client = AsyncMock()
            mock_http_client.post = AsyncMock(return_value=mock_response)
            mock_ensure.return_value = mock_http_client

            await client.reject_question("req_123", "/workdir/project")

        call_args = mock_http_client.post.call_args
        assert "/question/req_123/reject" in call_args[0][0]


class TestOpenCodeClientClose:
    """Tests for OpenCodeClient.close."""

    @pytest.mark.asyncio
    async def test_close_clears_state(self):
        """close clears all tracked state."""
        from pf_server.opencode_client import OpenCodeClient

        client = OpenCodeClient("http://localhost:5000")
        client._mcp_initialized.add("test")
        client._active_sessions["/test"] = "ses_test"

        # Create a mock client
        mock_http_client = AsyncMock()
        client._client = mock_http_client

        await client.close()

        assert client._client is None
        assert len(client._mcp_initialized) == 0
        assert len(client._active_sessions) == 0
        mock_http_client.aclose.assert_called_once()
