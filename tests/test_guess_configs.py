"""Tests for analysis configuration.

Tests cover:
- Base AnalysisConfig
- PFAnalysisConfig command formatting
- OpenCodeAnalysisConfig REST API integration
- Progress filters
- Config selectors
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestAnalysisConfigFormatFeedback:
    """Tests for AnalysisConfig.format_feedback."""

    def test_format_feedback_with_template(self):
        """Feedback template is formatted with kwargs."""
        from pf_server.guess_configs import PFAnalysisConfig

        config = PFAnalysisConfig(
            name="test",
            debounce_ms=100,
            command="pf",
            feedback_template="Changes:\n{changes}\n",
        )

        result = config.format_feedback(changes="file.py modified")

        assert result == "Changes:\nfile.py modified\n"

    def test_format_feedback_without_template(self):
        """Without template, returns kwargs as string."""
        from pf_server.guess_configs import PFAnalysisConfig

        config = PFAnalysisConfig(
            name="test",
            debounce_ms=100,
            command="pf",
            feedback_template=None,
        )

        result = config.format_feedback(changes="test", extra="data")

        assert "changes" in result
        assert "test" in result


class TestPFAnalysisConfigFormatCommand:
    """Tests for PFAnalysisConfig.format_command."""

    def test_format_command_with_placeholders(self):
        """Command placeholders are replaced with kwargs."""
        from pf_server.guess_configs import PFAnalysisConfig

        config = PFAnalysisConfig(
            name="test",
            debounce_ms=100,
            command="pf --config {config_name} --feedback {feedback_file}",
        )

        result = config.format_command(
            config_name="test.yaml",
            feedback_file="/path/to/feedback.json",
        )

        assert result == "pf --config test.yaml --feedback /path/to/feedback.json"

    def test_format_command_with_scope(self):
        """Scope is appended to command when set."""
        from pf_server.guess_configs import PFAnalysisConfig

        config = PFAnalysisConfig(
            name="test",
            debounce_ms=100,
            command="pf analyze",
            scope="module.py",
        )

        result = config.format_command()

        assert result == "pf analyze --scope module.py"

    def test_format_command_with_template_vars(self):
        """Template vars are merged with kwargs."""
        from pf_server.guess_configs import PFAnalysisConfig

        config = PFAnalysisConfig(
            name="test",
            debounce_ms=100,
            command="pf --model {model} --config {config_name}",
            template_vars={"model": "gpt-4"},
        )

        result = config.format_command(config_name="test.yaml")

        assert result == "pf --model gpt-4 --config test.yaml"


class TestPFConfigsFormatCorrectly:
    """Tests for actual PF analysis configs."""

    def test_lite_analysis_command_format(self):
        """LITE_ANALYSIS formats command with feedback file and config."""
        from pf_server.guess_configs import LITE_ANALYSIS

        result = LITE_ANALYSIS.format_command(
            feedback_file="/workdir/.pf/feedback.json",
            config_name="guesser-miner-v2.yaml",
        )

        assert "/workdir/.pf/feedback.json" in result
        assert "guesser-miner-v2.yaml" in result
        assert "pf" in result
        assert "--resume-with-feedback" in result

    def test_trigger_analysis_command_format(self):
        """TRIGGER_ANALYSIS formats command with config name."""
        from pf_server.guess_configs import TRIGGER_ANALYSIS

        result = TRIGGER_ANALYSIS.format_command(
            feedback_file="unused",
            config_name="guesser-miner-v2.yaml",
        )

        assert "guesser-miner-v2.yaml" in result
        assert "pf" in result
        assert "guess" in result

    def test_ask_analysis_command_format(self):
        """ASK_ANALYSIS_CONFIG formats command with question."""
        from pf_server.guess_configs import ASK_ANALYSIS_CONFIG

        result = ASK_ANALYSIS_CONFIG.format_command(
            feedback_file="unused",
            config_name="ask-question-guess.yaml",
            question="Is this function pure?",
        )

        assert "ask-question-guess.yaml" in result
        assert "Is this function pure?" in result


class TestProgressFilters:
    """Tests for progress filter functions."""

    def test_lite_progress_filter_matches_agent_events(self):
        """lite_progress_filter matches agent_* events."""
        from pf_server.guess_configs import lite_progress_filter

        assert lite_progress_filter({"event": "agent_step"}) is True
        assert lite_progress_filter({"event": "agent_complete"}) is True
        assert lite_progress_filter({"event": "agent_start"}) is True

    def test_lite_progress_filter_matches_max_iterations(self):
        """lite_progress_filter matches max_iterations_reached."""
        from pf_server.guess_configs import lite_progress_filter

        assert lite_progress_filter({"event": "max_iterations_reached"}) is True

    def test_lite_progress_filter_rejects_other_events(self):
        """lite_progress_filter rejects non-matching events."""
        from pf_server.guess_configs import lite_progress_filter

        assert lite_progress_filter({"event": "some_other_event"}) is False
        assert lite_progress_filter({"event": ""}) is False
        assert lite_progress_filter({}) is False

    def test_opencode_progress_filter_matches_progress_events(self):
        """opencode_progress_filter matches session.status and file.edited."""
        from pf_server.guess_configs import opencode_progress_filter

        # These are the actual SSE event types from OpenCode
        assert opencode_progress_filter({"type": "session.status"}) is True
        assert opencode_progress_filter({"type": "file.edited"}) is True

    def test_opencode_progress_filter_rejects_other_events(self):
        """opencode_progress_filter rejects non-progress events."""
        from pf_server.guess_configs import opencode_progress_filter

        assert opencode_progress_filter({"type": "server.connected"}) is False
        assert opencode_progress_filter({"type": "server.heartbeat"}) is False
        assert opencode_progress_filter({"type": "message.part.updated"}) is False
        assert opencode_progress_filter({"type": "session.updated"}) is False
        assert opencode_progress_filter({"type": ""}) is False
        assert opencode_progress_filter({}) is False


class TestPFAnalysisConfigCancel:
    """Tests for PFAnalysisConfig.cancel method."""

    @pytest.mark.asyncio
    async def test_cancel_sends_sigterm_via_pkill(self):
        """PF cancel sends SIGTERM via pkill with marker."""
        from pf_server.guess_configs import AnalysisContext, PFAnalysisConfig

        config = PFAnalysisConfig(
            name="test",
            debounce_ms=100,
            command="pf analyze",
        )
        config._current_marker = "pf_analysis_test_abc123"

        mock_container = MagicMock()
        mock_result = MagicMock()
        mock_result.exit_code = 0
        mock_container.exec_run = MagicMock(return_value=mock_result)

        ctx = AnalysisContext(
            container=mock_container,
            project_path="/workdir",
        )

        result = await config.cancel(ctx)

        assert result is True
        mock_container.exec_run.assert_called_once()
        call_args = mock_container.exec_run.call_args
        assert call_args[1]["cmd"] == [
            "pkill",
            "-TERM",
            "-f",
            "pf_analysis_test_abc123",
        ]

    @pytest.mark.asyncio
    async def test_cancel_returns_true_when_no_marker(self):
        """Cancel returns True when no marker is set."""
        from pf_server.guess_configs import AnalysisContext, PFAnalysisConfig

        config = PFAnalysisConfig(
            name="test",
            debounce_ms=100,
            command="pf analyze",
        )
        # No _current_marker set

        mock_container = MagicMock()
        ctx = AnalysisContext(
            container=mock_container,
            project_path="/workdir",
        )

        result = await config.cancel(ctx)

        assert result is True
        mock_container.exec_run.assert_not_called()


class TestOpenCodeAnalysisConfigCancel:
    """Tests for OpenCodeAnalysisConfig.cancel method."""

    @pytest.mark.asyncio
    async def test_cancel_calls_client_abort(self):
        """OpenCode cancel calls client's abort method."""
        from pf_server.guess_configs import AnalysisContext, OpenCodeAnalysisConfig

        config = OpenCodeAnalysisConfig(
            name="test_opencode",
            debounce_ms=100,
        )

        mock_container = MagicMock()
        mock_client = AsyncMock()
        mock_client.abort = AsyncMock(return_value=True)

        ctx = AnalysisContext(
            container=mock_container,
            project_path="/workdir/project",
            opencode_client=mock_client,
        )

        result = await config.cancel(ctx)

        assert result is True
        mock_client.abort.assert_called_once_with("/workdir/project")

    @pytest.mark.asyncio
    async def test_cancel_returns_true_when_no_client(self):
        """OpenCode cancel returns True when no client."""
        from pf_server.guess_configs import AnalysisContext, OpenCodeAnalysisConfig

        config = OpenCodeAnalysisConfig(
            name="test_opencode",
            debounce_ms=100,
        )

        mock_container = MagicMock()
        ctx = AnalysisContext(
            container=mock_container,
            project_path="/workdir/project",
            opencode_client=None,
        )

        result = await config.cancel(ctx)

        assert result is True


class TestConfigSelectors:
    """Tests for config selector functions."""

    def test_get_lite_analysis_config_returns_pf_by_default(self):
        """get_lite_analysis_config returns PF config by default."""
        from pf_server.guess_configs import LITE_ANALYSIS, get_lite_analysis_config

        with patch("pf_server.guess_configs.settings") as mock_settings:
            mock_settings.lite_analysis_backend = "pf"
            config = get_lite_analysis_config()
            assert config is LITE_ANALYSIS

    def test_get_lite_analysis_config_returns_opencode_when_configured(self):
        """get_lite_analysis_config returns OpenCode config when backend is opencode."""
        from pf_server.guess_configs import (
            OPENCODE_LITE_ANALYSIS,
            get_lite_analysis_config,
        )

        with patch("pf_server.guess_configs.settings") as mock_settings:
            mock_settings.lite_analysis_backend = "opencode"
            config = get_lite_analysis_config()
            assert config is OPENCODE_LITE_ANALYSIS

    def test_get_trigger_analysis_config_returns_pf_by_default(self):
        """get_trigger_analysis_config returns PF config by default."""
        from pf_server.guess_configs import (
            TRIGGER_ANALYSIS,
            get_trigger_analysis_config,
        )

        with patch("pf_server.guess_configs.settings") as mock_settings:
            mock_settings.trigger_analysis_backend = "pf"
            config = get_trigger_analysis_config()
            assert config is TRIGGER_ANALYSIS

    def test_get_trigger_analysis_config_returns_opencode_when_configured(self):
        """get_trigger_analysis_config returns OpenCode config when backend is opencode."""
        from pf_server.guess_configs import (
            OPENCODE_TRIGGER_ANALYSIS,
            get_trigger_analysis_config,
        )

        with patch("pf_server.guess_configs.settings") as mock_settings:
            mock_settings.trigger_analysis_backend = "opencode"
            config = get_trigger_analysis_config()
            assert config is OPENCODE_TRIGGER_ANALYSIS

    def test_get_ask_analysis_config_returns_pf_by_default(self):
        """get_ask_analysis_config returns PF config by default."""
        from pf_server.guess_configs import (
            ASK_ANALYSIS_CONFIG,
            get_ask_analysis_config,
        )

        with patch("pf_server.guess_configs.settings") as mock_settings:
            mock_settings.ask_analysis_backend = "pf"
            config = get_ask_analysis_config()
            assert config is ASK_ANALYSIS_CONFIG

    def test_get_ask_analysis_config_returns_opencode_when_configured(self):
        """get_ask_analysis_config returns OpenCode config when backend is opencode."""
        from pf_server.guess_configs import (
            OPENCODE_ASK_ANALYSIS,
            get_ask_analysis_config,
        )

        with patch("pf_server.guess_configs.settings") as mock_settings:
            mock_settings.ask_analysis_backend = "opencode"
            config = get_ask_analysis_config()
            assert config is OPENCODE_ASK_ANALYSIS

    def test_mixed_backends_work_independently(self):
        """Each analysis type can use a different backend."""
        from pf_server.guess_configs import (
            ASK_ANALYSIS_CONFIG,
            OPENCODE_LITE_ANALYSIS,
            OPENCODE_TRIGGER_ANALYSIS,
            get_ask_analysis_config,
            get_lite_analysis_config,
            get_trigger_analysis_config,
        )

        with patch("pf_server.guess_configs.settings") as mock_settings:
            # Configure mixed backends: lite=opencode, trigger=opencode, ask=pf
            mock_settings.lite_analysis_backend = "opencode"
            mock_settings.trigger_analysis_backend = "opencode"
            mock_settings.ask_analysis_backend = "pf"

            assert get_lite_analysis_config() is OPENCODE_LITE_ANALYSIS
            assert get_trigger_analysis_config() is OPENCODE_TRIGGER_ANALYSIS
            assert get_ask_analysis_config() is ASK_ANALYSIS_CONFIG
