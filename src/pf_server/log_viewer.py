"""Pretty log viewer for pf-server JSON logs.

Reads JSON logs from stdin and renders them with Rich styling.
Uses the same formatting as local development mode via shared ConsoleRenderer.

Usage:
    journalctl -u pf-server -f | pf-logs
    tail -f /var/log/pf-server.log | pf-logs
    cat logs.json | pf-logs
"""

import json
import sys

from pf_server.logging_config import (
    RichStyledLogger,
    get_console_renderer,
)


def main() -> None:
    """Main entry point for the log viewer."""
    # Use the same ConsoleRenderer as local dev
    renderer = get_console_renderer()
    logger = RichStyledLogger()

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            try:
                entry = json.loads(line)
                # ConsoleRenderer expects 'level' key, but JSONRenderer outputs 'log_level'
                if "log_level" in entry:
                    entry["level"] = entry.pop("log_level")
                # Run through ConsoleRenderer to get formatted output
                # ConsoleRenderer expects (logger, method_name, event_dict)
                formatted = renderer(None, entry.get("level", "info"), entry)
                logger.msg(formatted)

            except json.JSONDecodeError:
                # Not JSON - print raw line dimmed
                print(f"\x1b[2m{line}\x1b[0m")

    except KeyboardInterrupt:
        # Clean exit on Ctrl+C
        pass
    except BrokenPipeError:
        # Handle pipe closed (e.g., piping to head)
        pass


if __name__ == "__main__":
    main()
