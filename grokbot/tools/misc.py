"""Small built-in tools that need no sandbox."""
import datetime

from agent_loop import ToolError as _WorkspaceError, calculate as _calculate
from grokbot.tools import ToolError


def register(registry):
    @registry.tool('calculate', 'Evaluate arithmetic exactly (+ - * / // % and parentheses). Prefer run_command with python for anything else.',
                   {'type': 'object', 'properties': {'expression': {'type': 'string', 'maxLength': 200}},
                    'required': ['expression']}, category='misc')
    def calculate(ctx, expression):
        try:
            return str(_calculate(expression))
        except _WorkspaceError as exc:
            raise ToolError(str(exc)) from exc

    @registry.tool('current_time', 'Current date and time (UTC and local server time).',
                   {'type': 'object', 'properties': {}, 'required': []}, category='misc')
    def current_time(ctx):
        now = datetime.datetime.now(datetime.timezone.utc)
        return 'UTC: ' + now.strftime('%Y-%m-%d %H:%M:%S') + ' · local: ' + now.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')
