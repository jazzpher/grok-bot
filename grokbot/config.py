"""Environment-driven limits and defaults. Every value has a safe default.

Legacy defaults (25 files / 32 KB / 256 KB / 12 steps / 24 000 chars) were far
below what a coding or research agent needs. The new defaults are generous but
still bounded; operators can lower or raise them with the GROK_* variables.
"""
import os
import shutil


def _int(name, default, minimum=0):
    try:
        value = int(os.environ.get(name, '') or default)
    except ValueError:
        value = default
    return max(minimum, value)


def _choice(name, default, allowed):
    value = (os.environ.get(name, '') or default).strip().lower()
    return value if value in allowed else default


# Workspace limits
MAX_FILE_BYTES = _int('GROK_MAX_FILE_BYTES', 10 * 1024 * 1024, 1024)
MAX_WORKSPACE_BYTES = _int('GROK_MAX_WORKSPACE_BYTES', 500 * 1024 * 1024, 65536)
MAX_FILES = _int('GROK_MAX_FILES', 2000, 1)
MAX_READ_BYTES = _int('GROK_MAX_READ_BYTES', 2 * 1024 * 1024, 1024)

# Agent loop
MAX_STEPS = _int('GROK_MAX_STEPS', 50, 1)
MAX_TOOL_OUTPUT_CHARS = _int('GROK_MAX_TOOL_OUTPUT', 20000, 500)
MAX_PARALLEL_TOOLS = _int('GROK_MAX_PARALLEL_TOOLS', 8, 1)
DEFAULT_CONTEXT_TOKENS = _int('GROK_CONTEXT_TOKENS', 128000, 4000)
COMPACT_RATIO = 0.70          # summarize when estimated tokens exceed this share of the window
KEEP_RECENT_MESSAGES = 8      # always keep the newest messages verbatim after compaction
TOOL_MODE = _choice('GROK_TOOL_MODE', 'auto', ('auto', 'native', 'json'))

# Sandbox
SANDBOX_BACKEND = _choice('GROK_SANDBOX', 'auto', ('auto', 'docker', 'subprocess', 'off'))
SANDBOX_IMAGE = os.environ.get('GROK_SANDBOX_IMAGE', 'python:3.12-slim')
COMMAND_TIMEOUT = _int('GROK_COMMAND_TIMEOUT', 120, 1)
COMMAND_MAX_OUTPUT = _int('GROK_COMMAND_MAX_OUTPUT', 60000, 1000)
SANDBOX_MEMORY = os.environ.get('GROK_SANDBOX_MEMORY', '1g')
SANDBOX_CPUS = os.environ.get('GROK_SANDBOX_CPUS', '1.0')
SANDBOX_NETWORK = _choice('GROK_SANDBOX_NETWORK', 'off', ('off', 'on'))
DEFAULT_SANDBOX_MODE = _choice('GROK_SANDBOX_MODE', 'workspace-write', ('read-only', 'workspace-write', 'full-access'))
DEFAULT_APPROVAL = _choice('GROK_APPROVAL', 'on-request', ('on-request', 'never'))
APPROVAL_TIMEOUT = _int('GROK_APPROVAL_TIMEOUT', 600, 10)

# Web tools
SEARCH_BACKEND = _choice('GROK_SEARCH_BACKEND', 'auto', ('auto', 'tavily', 'brave', 'searxng', 'duckduckgo', 'off'))
TAVILY_API_KEY = os.environ.get('TAVILY_API_KEY', '')
BRAVE_API_KEY = os.environ.get('BRAVE_API_KEY', '')
SEARXNG_URL = os.environ.get('SEARXNG_URL', '')
FETCH_MAX_BYTES = _int('GROK_FETCH_MAX_BYTES', 3 * 1024 * 1024, 65536)
FETCH_MAX_CHARS = _int('GROK_FETCH_MAX_CHARS', 40000, 1000)
FETCH_TIMEOUT = _int('GROK_FETCH_TIMEOUT', 20, 3)


def docker_available():
    return shutil.which('docker') is not None


def resolve_sandbox_backend():
    if SANDBOX_BACKEND == 'auto':
        return 'docker' if docker_available() else 'subprocess'
    return SANDBOX_BACKEND
