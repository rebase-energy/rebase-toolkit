"""What this process tells the platform about itself on every request.

Four facts, none of them about the user's code or data: which client this is
(``cli``, ``tui`` or ``sdk`` -- the SDK imported by a script or notebook), which
CLI command was typed (the command path only, never its arguments), the toolkit
version, and an id minted once per process. The id lets the platform count one
``rebase deploy`` as one deploy even though it is several requests, and one TUI
session as one session even though it is a poll every ten seconds.

Process-global on purpose: there are ~100 bare ``Client()`` constructions in the
CLI, and threading a surface through each would be the kind of wiring that gets
forgotten. The CLI sets it once in ``main``; everything else reads it here.
"""

from __future__ import annotations

import os
import platform
import sys
from uuid import uuid4

from rebase.version import __version__

CLIENT_SDK = "sdk"
CLIENT_CLI = "cli"
CLIENT_TUI = "tui"

# Inside a platform container the SDK is talking on behalf of a run, not a person.
_RUN_ID_ENV = "REBASE_RUN_ID"

_invocation_id = str(uuid4())
_client = CLIENT_SDK
_command: str | None = None


def set_client(client: str, *, command: str | None = None) -> None:
    global _client, _command
    _client = client
    if command is not None:
        _command = command


def user_agent() -> str:
    python = ".".join(str(part) for part in sys.version_info[:3])
    return f"rebase-toolkit/{__version__} (python {python}; {platform.system().lower() or 'unknown'})"


def identity_headers() -> dict[str, str]:
    headers = {
        "User-Agent": user_agent(),
        "X-Rebase-Client": _client,
        "X-Rebase-Client-Version": __version__,
        "X-Rebase-Invocation": _invocation_id,
        "X-Rebase-Origin": "run" if os.getenv(_RUN_ID_ENV) else "local",
    }
    if _command:
        headers["X-Rebase-Command"] = _command
    return headers


def command_path(group: object, args: list[str], *, prefix: str = "") -> str | None:
    """The command names in *args*, walked against the click command tree.

    Only tokens that name a registered command are kept, so a file path, a
    target, a secret name or any other argument can never end up in the header:
    ``rebase workflow schedule set nightly --cron ...`` is ``workflow schedule set``.
    """
    names: list[str] = [prefix] if prefix else []
    current = group
    for token in args:
        commands = getattr(current, "commands", None)
        if not isinstance(commands, dict):
            break
        if token.startswith("-"):
            # A group-level option such as --workspace; its value is skipped by
            # the membership test on the next token.
            continue
        if token not in commands:
            if names:
                break
            continue
        names.append(token)
        current = commands[token]
    return " ".join(names) or None
