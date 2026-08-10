"""Terminal bridge for ``rebase shell``: local TTY <-> relay WebSocket.

The relay pairs this client with the shell agent running inside the cloud
container. Text frames are JSON control messages, binary frames are raw PTY
bytes (protocol shared with the platform's terminal service):

  client -> {"type": "auth", "token": ..., "session_id": ...}   first frame
  relay  -> {"type": "ready"}          agent attached; PTY bytes follow
  client -> {"type": "resize", "cols": C, "rows": R}
  both   -> binary                     PTY bytes / keystrokes

POSIX-only: raw mode needs termios, and resize propagation needs SIGWINCH.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass

from .client import RebaseWorkflowError

# Close codes shared with the relay.
CLOSE_MESSAGES = {
    4400: "the relay rejected the connection (protocol error)",
    4401: "the relay rejected the connection (unauthenticated)",
    4403: "the session is expired, already used, or unavailable",
    4409: "another client is already attached to this session",
}


@dataclass
class ShellResult:
    close_code: int | None
    close_reason: str


def close_message(result: ShellResult) -> str | None:
    """Human-readable explanation for an abnormal close, if any."""
    if result.close_code in CLOSE_MESSAGES:
        return CLOSE_MESSAGES[result.close_code]
    if result.close_code not in (None, 1000, 1001):
        reason = f": {result.close_reason}" if result.close_reason else ""
        return f"connection closed unexpectedly (code {result.close_code}{reason})"
    return None


def _require_posix_tty() -> None:
    try:
        import termios  # noqa: F401
    except ImportError as error:  # pragma: no cover - Windows
        raise RebaseWorkflowError("rebase shell requires a POSIX terminal (termios is unavailable)") from error
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RebaseWorkflowError("rebase shell must run in an interactive terminal")


async def _run(
    relay_ws_url: str,
    session_id: str,
    client_token: str,
    *,
    on_waiting: Callable[[], None] | None,
    on_ready: Callable[[], None] | None,
) -> ShellResult:
    import termios
    import tty

    import websockets

    async with websockets.connect(relay_ws_url, max_size=2**20) as ws:
        await ws.send(json.dumps({"type": "auth", "token": client_token, "session_id": session_id}))
        if on_waiting is not None:
            on_waiting()
        # "ready" arrives once the container dials in — minutes on a cold start
        # with dependency installs. The library's protocol-level pings keep the
        # connection alive through proxies while we wait.
        while True:
            message = await ws.recv()
            if isinstance(message, str) and json.loads(message).get("type") == "ready":
                break
        if on_ready is not None:
            on_ready()

        loop = asyncio.get_running_loop()
        stdin_fd = sys.stdin.fileno()
        saved_termios = termios.tcgetattr(stdin_fd)

        async def send_resize() -> None:
            size = shutil.get_terminal_size()
            await ws.send(json.dumps({"type": "resize", "cols": size.columns, "rows": size.lines}))

        def on_stdin_readable() -> None:
            data = sys.stdin.buffer.raw.read(65536)  # type: ignore[attr-defined]
            if data:
                asyncio.ensure_future(ws.send(data))

        def on_winch() -> None:
            asyncio.ensure_future(send_resize())

        tty.setraw(stdin_fd)
        loop.add_reader(stdin_fd, on_stdin_readable)
        loop.add_signal_handler(signal.SIGWINCH, on_winch)
        try:
            await send_resize()
            async for message in ws:
                if isinstance(message, bytes):
                    sys.stdout.buffer.write(message)
                    sys.stdout.buffer.flush()
                # Control frames (pong etc.) need no handling here.
        except Exception:  # noqa: BLE001 - the close code carries the story
            pass
        finally:
            loop.remove_signal_handler(signal.SIGWINCH)
            loop.remove_reader(stdin_fd)
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved_termios)
        with contextlib.suppress(Exception):
            await ws.close()
        return ShellResult(close_code=ws.close_code, close_reason=ws.close_reason or "")


def run_bridge(
    relay_ws_url: str,
    session_id: str,
    client_token: str,
    *,
    on_waiting: Callable[[], None] | None = None,
    on_ready: Callable[[], None] | None = None,
) -> ShellResult:
    """Bridge the local terminal to the shell session. Blocks until it ends."""
    _require_posix_tty()
    return asyncio.run(_run(relay_ws_url, session_id, client_token, on_waiting=on_waiting, on_ready=on_ready))
