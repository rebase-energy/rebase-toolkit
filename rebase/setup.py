from __future__ import annotations

import copy
import os
import re
import select
import subprocess
import sys
import threading
import time
import webbrowser
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from rebase.auth import (
    AuthError,
    build_supabase_authorize_url,
    clear_session,
    exchange_pkce_code,
    fetch_link_identity_url,
    fetch_user_identities,
    generate_pkce_verifier,
    github_username_from_jwt,
    load_access_token,
    load_session,
    pkce_challenge,
    refresh_session,
    save_session,
)
from rebase.brand import BRAND_BRIGHT_GREEN, BRAND_MEDIUM_GRAY
from rebase.client import Client, RebaseWorkflowError, _git, _parse_github_remote
from rebase.config import selected_profile_name, write_local_config, write_profile

JOIN_WORKSPACE = "Join an existing workspace"
CREATE_WORKSPACE = "Create a new workspace"
SWITCH_ACCOUNT = "Sign in with a different account"
TRY_ANOTHER_HANDLE = "Enter a different workspace handle"
BETA_ENROLLMENT_ERROR = "your account is not enrolled in the beta program. Contact hello@rebase.energy to get enrolled."
WORKSPACE_CREATION_QUOTA_ERROR = (
    "You've reached your quota for creating new workspaces, please contact us to increase it: hello@rebase.energy"
)
HANDLE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{1,37}[a-z0-9])?$")
HUGGINGFACE_CLIENT_ID_ENV = "REBASE_HUGGINGFACE_OAUTH_CLIENT_ID"
HUGGINGFACE_DEVICE_URL = "https://huggingface.co/oauth/device"
HUGGINGFACE_TOKEN_URL = "https://huggingface.co/oauth/token"
HUGGINGFACE_DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
HUGGINGFACE_DEFAULT_SCOPES = ("openid", "profile", "email", "write-repos")


class _SwitchAccountRequested(Exception):
    """Raised when someone picks `SWITCH_ACCOUNT` deeper in the flow.

    Signing in again cannot happen where the choice is made — it invalidates the
    client every caller below is holding — so the request travels up to `run_setup`,
    which re-authenticates and re-runs the step. Deliberately not a
    `RebaseWorkflowError`: it is a control signal, and must never be mistaken for a
    failure worth reporting.
    """


class _CallbackHandler(BaseHTTPRequestHandler):
    server: _CallbackServer

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        params = parse_qs(parsed.query)
        self.server.callback_params = {key: values[-1] for key, values in params.items() if values}
        error = self.server.callback_params.get("error")
        if error:
            message = self.server.callback_params.get("error_description") or error
            body = f"Authentication failed: {escape(message)}. You can close this tab and return to the terminal."
        else:
            body = "Authentication complete. You can close this tab."
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        close_script = (
            ""
            if error
            else """
    <script>
      window.setTimeout(function () {
        window.close();
      }, 100);
    </script>"""
        )
        self.wfile.write(
            f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>Authentication</title>
    <style>
      html, body {{
        height: 100%;
        margin: 0;
      }}
      body {{
        align-items: center;
        background: #ffffff;
        color: #111111;
        display: flex;
        font: 18px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        justify-content: center;
        text-align: center;
      }}
    </style>
  </head>
  <body>
    <main>{body}</main>{close_script}
  </body>
</html>""".encode()
        )
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def log_message(self, format: str, *args: Any) -> None:
        return


class _CallbackServer(HTTPServer):
    callback_params: dict[str, str] | None = None


def _ansi_color(hex_color: str) -> str:
    hex_value = hex_color.lstrip("#")
    red = int(hex_value[0:2], 16)
    green = int(hex_value[2:4], 16)
    blue = int(hex_value[4:6], 16)
    return f"\033[38;2;{red};{green};{blue}m"


RESET = "\033[0m"
GREEN = _ansi_color(BRAND_BRIGHT_GREEN)
MUTED = _ansi_color(BRAND_MEDIUM_GRAY)
ERROR = _ansi_color("#ff5c5c")
BOLD = "\033[1m"
DIM = "\033[2m"
SELECTED_MARKER = f"{_ansi_color(BRAND_BRIGHT_GREEN)}●{RESET}"
UNSELECTED_MARKER = f"{_ansi_color(BRAND_MEDIUM_GRAY)}○{RESET}"
SETUP_STEPS = ("Authenticate", "Workspace")
SECTION_RULE = "─" * 72


def _paint(text: str, *styles: str) -> str:
    return "".join(styles) + text + RESET


def _intro() -> None:
    print(_paint("Rebase setup", BOLD, GREEN))
    print(_paint("Connect your identity and workspace.", DIM, MUTED))
    print()


def _section(title: str, *, step: int | None = None, total: int | None = None) -> None:
    label = title if step is None or total is None else f"Step {step} of {total}: {title}"
    print()
    print(_paint(SECTION_RULE, DIM, MUTED))
    print(f"{SELECTED_MARKER} {_paint(label, BOLD, GREEN)}")
    print(_paint(SECTION_RULE, DIM, MUTED))


def _success(message: str) -> None:
    print(f"{_paint('✓', GREEN)} {message}")


def _failure(message: str) -> None:
    print(f"{_paint('✗', ERROR)} {message}")


def _hint(message: str) -> None:
    print(_paint(message, DIM, MUTED))


def _can_prompt() -> bool:
    """Whether there is someone at the keyboard to answer a question.

    Piped and scripted runs must not be stopped by a menu, so a question that is
    only ever an offer — not something the flow needs an answer to — is skipped
    when stdin is not a terminal.
    """
    try:
        return sys.stdin.isatty()
    except ValueError:
        return False


def _session_description(session: Any | None) -> str:
    """Who a stored session belongs to, named the way an invite would name them."""
    if session is None:
        return "an unknown account"
    who = getattr(session, "email", None) or getattr(session, "user_id", None) or "Supabase user"
    provider = getattr(session, "provider", None)
    return f"{who} (via {provider})" if provider else str(who)


def _terminal_fd() -> tuple[int | None, bool]:
    try:
        return os.open("/dev/tty", os.O_RDWR), True
    except OSError:
        if sys.stdin.isatty():
            return sys.stdin.fileno(), False
    return None, False


def _restore_terminal_for_prompts() -> None:
    try:
        import termios
    except ImportError:
        return

    fd, should_close = _terminal_fd()
    if fd is None:
        return
    try:
        attrs = termios.tcgetattr(fd)
        attrs[0] |= termios.ICRNL
        attrs[3] |= termios.ECHO | termios.ICANON | termios.IEXTEN | termios.ISIG
        attrs[6][termios.VINTR] = b"\x03" if isinstance(attrs[6][termios.VINTR], bytes) else 3
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except OSError:
        return
    finally:
        if should_close:
            os.close(fd)


def _read_tty_line(prompt: str) -> str | None:
    try:
        import termios
    except ImportError:
        return None

    fd, should_close = _terminal_fd()
    if fd is None:
        return None

    try:
        old_attrs = termios.tcgetattr(fd)
    except OSError:
        if should_close:
            os.close(fd)
        return None

    attrs = old_attrs[:]
    attrs[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG)
    attrs[6][termios.VMIN] = 1
    attrs[6][termios.VTIME] = 0
    buffer = bytearray()
    try:
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        os.write(fd, prompt.encode())
        while True:
            char = os.read(fd, 1)
            if char == b"\x03":
                os.write(fd, b"^C\n")
                raise KeyboardInterrupt
            if char in {b"\r", b"\n"}:
                os.write(fd, b"\n")
                return buffer.decode(errors="ignore")
            if char in {b"\x7f", b"\b"}:
                if buffer:
                    del buffer[-1]
                    os.write(fd, b"\b \b")
                continue
            if char == b"\x04" and not buffer:
                raise KeyboardInterrupt
            buffer.extend(char)
            os.write(fd, char)
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, old_attrs)
        if should_close:
            os.close(fd)


def _read_input(prompt: str) -> str:
    _restore_terminal_for_prompts()
    tty_value = _read_tty_line(prompt)
    if tty_value is not None:
        return tty_value
    try:
        entered = input(prompt)
    except EOFError:
        raise KeyboardInterrupt from None
    if "\x03" in entered:
        raise KeyboardInterrupt
    return entered


def _selector_lines(title: str, values: list[str], selected_index: int) -> list[str]:
    lines = [title]
    for index, value in enumerate(values):
        marker = SELECTED_MARKER if index == selected_index else UNSELECTED_MARKER
        lines.append(f"  {marker} {value}")
    return lines


def _selector_index_for_key(selected_index: int, key: bytes, count: int) -> int:
    if key in {b"\x1b[A", b"k"}:
        return (selected_index - 1) % count
    if key in {b"\x1b[B", b"j"}:
        return (selected_index + 1) % count
    return selected_index


def _read_tty_key(fd: int) -> bytes:
    key = os.read(fd, 1)
    if key != b"\x1b":
        return key
    suffix = bytearray()
    for _ in range(2):
        ready, _, _ = select.select([fd], [], [], 0.05)
        if not ready:
            break
        suffix.extend(os.read(fd, 1))
    return key + bytes(suffix)


def _render_selector(fd: int, title: str, values: list[str], selected_index: int, *, previous_lines: int) -> int:
    lines = _selector_lines(title, values, selected_index)
    if previous_lines:
        os.write(fd, f"\033[{previous_lines}F".encode())
    for line in lines:
        os.write(fd, f"\033[2K{line}\r\n".encode())
    return len(lines)


def _choose_tty(title: str, values: list[str], *, default: str) -> str | None:
    try:
        import termios
    except ImportError:
        return None

    fd, should_close = _terminal_fd()
    if fd is None:
        return None

    try:
        old_attrs = termios.tcgetattr(fd)
    except OSError:
        if should_close:
            os.close(fd)
        return None

    selected_index = values.index(default)
    previous_lines = 0
    attrs = old_attrs[:]
    attrs[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG)
    attrs[6][termios.VMIN] = 1
    attrs[6][termios.VTIME] = 0
    try:
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        os.write(fd, b"\033[?25l")
        previous_lines = _render_selector(fd, title, values, selected_index, previous_lines=previous_lines)
        while True:
            key = _read_tty_key(fd)
            if key == b"\x03":
                os.write(fd, b"^C\n")
                raise KeyboardInterrupt
            if key in {b"\r", b"\n"}:
                return values[selected_index]
            if key == b"\x04":
                raise KeyboardInterrupt
            next_index = _selector_index_for_key(selected_index, key, len(values))
            if next_index != selected_index:
                selected_index = next_index
                previous_lines = _render_selector(fd, title, values, selected_index, previous_lines=previous_lines)
    finally:
        os.write(fd, b"\033[?25h")
        termios.tcsetattr(fd, termios.TCSANOW, old_attrs)
        if should_close:
            os.close(fd)


def _prompt(value: str | None, message: str, *, default: str | None = None) -> str:
    """Ask for `message`, spelling out what pressing enter will do.

    A bare `[value]` suffix leaves people guessing whether it is a placeholder, an
    example, or something that will actually be used, so the default is stated as
    the action it performs.
    """
    if value:
        return value
    suffix = f' (enter to use "{default}")' if default else ""
    entered = _read_input(f"{message}{suffix}: ").strip()
    if entered:
        return entered
    if default is not None:
        return default
    raise RebaseWorkflowError(f"{message} is required")


def _normalize_handle(value: str, *, label: str) -> str:
    handle = value.strip().lower()
    if not HANDLE_RE.fullmatch(handle):
        raise RebaseWorkflowError(
            f"{label} must be 3-39 characters and contain only letters, numbers, hyphens, or underscores"
        )
    return handle


def _directory_handle_suggestion(directory: str | Path | None = None) -> str | None:
    """The current directory's name as a workspace handle, when it can be one.

    A workspace is 1:1 with a repo, so the folder you run `rebase setup` in is a far
    better guess than the user handle — which is only ever right for the first
    workspace someone creates. Returns None when the name cannot be normalized into
    a valid handle, leaving the caller's fallback in place.
    """
    try:
        name = Path(directory).expanduser().resolve().name if directory else Path.cwd().resolve().name
    except OSError:
        return None
    suggestion = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-_")
    if HANDLE_RE.fullmatch(suggestion):
        return suggestion
    return None


def _handle_suggestion(session: Any | None) -> str | None:
    email = getattr(session, "email", None)
    if not isinstance(email, str) or "@" not in email:
        return None
    local_part = email.split("@", 1)[0].lower()
    suggestion = re.sub(r"[^a-z0-9_-]+", "-", local_part).strip("-_")
    if HANDLE_RE.fullmatch(suggestion):
        return suggestion
    return None


def _get_my_profile(client: Client) -> dict[str, Any]:
    response = client.request("GET", "/me/profile")
    if not isinstance(response, dict):
        raise RebaseWorkflowError("expected profile response")
    return response


def _update_my_profile(client: Client, *, handle: str) -> dict[str, Any]:
    response = client.request("PATCH", "/me/profile", json={"handle": handle})
    if not isinstance(response, dict):
        raise RebaseWorkflowError("expected profile response")
    return response


def _ensure_profile_handle(args: Any, client: Client, *, session: Any | None) -> dict[str, Any]:
    profile = _get_my_profile(client)
    existing_handle = profile.get("handle")
    if isinstance(existing_handle, str) and existing_handle:
        return profile
    handle = _prompt(getattr(args, "handle", None), "Choose your Rebase handle", default=_handle_suggestion(session))
    normalized_handle = _normalize_handle(handle, label="Rebase handle")
    profile = _update_my_profile(client, handle=normalized_handle)
    _success(f"Using Rebase handle @{profile['handle']}")
    return profile


def _confirm(message: str, *, default: bool) -> bool:
    default_value = "Yes" if default else "No"
    return _choose("answer", ["Yes", "No"], default=default_value, title=message) == "Yes"


def _choose(label: str, values: list[str], *, default: str | None = None, title: str | None = None) -> str:
    if not values:
        raise RebaseWorkflowError(f"no {label} options available")
    default = default if default in values else values[0]
    title = title or f"Choose {label}:"
    selected = _choose_tty(title, values, default=default)
    if selected is not None:
        return selected

    print(title)
    for index, value in enumerate(values, start=1):
        marker = " (default)" if value == default else ""
        print(f"  {index}. {value}{marker}")
    entered = _read_input(f"{label} [1-{len(values)}]: ").strip()
    if not entered:
        return default
    try:
        selected = values[int(entered) - 1]
    except (ValueError, IndexError) as exc:
        raise RebaseWorkflowError(f"invalid {label} selection") from exc
    return selected


def _run_git(args: list[str], *, cwd: Path, action: str, timeout: float = 60) -> None:
    try:
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=timeout)
    except OSError as exc:
        raise RebaseWorkflowError(f"failed to {action}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RebaseWorkflowError(f"timed out while trying to {action}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        if "Password authentication is not supported for Git operations" in detail:
            detail += (
                "\nGitHub no longer accepts account passwords for HTTPS Git operations. "
                "Use SSH, a GitHub token through your credential manager, or switch this repo's origin to SSH."
            )
        suffix = f": {detail}" if detail else ""
        raise RebaseWorkflowError(f"failed to {action}{suffix}") from exc


def _local_github_remote(cwd: Path | None = None) -> str | None:
    owner, name = _parse_github_remote(_git(["config", "--get", "remote.origin.url"], cwd=cwd))
    return f"{owner}/{name}" if owner and name else None


def _current_git_root() -> Path | None:
    root = _git(["rev-parse", "--show-toplevel"], cwd=Path.cwd())
    return Path(root).resolve() if root else None


def _mark_local_workspace(workspace: dict[str, Any]) -> Path | None:
    """Write the `.rebase/config.json` marker pinning this repo to `workspace`.

    Anchored at the git root rather than the cwd, so running setup from a
    subdirectory still marks the repo once and every subdirectory resolves to it.
    Failure to write is a hint, not an error: the global profile was already saved,
    so the workspace is usable either way.
    """
    directory = _current_git_root() or Path.cwd()
    workspace_id = str(workspace["id"])
    name = workspace.get("name")
    try:
        marker = write_local_config(
            directory,
            workspace_id=workspace_id,
            workspace_name=name if isinstance(name, str) and name else None,
        )
    except OSError as exc:
        _hint(f"Could not write the workspace marker in {directory}: {exc}")
        return None
    _success(f"Marked {directory.name} as workspace {workspace_id}")
    _hint(f"Wrote {marker} — commit it so the repo always resolves to this workspace")
    return marker


def _validate_github_repo_full_name(value: str) -> str:
    repo_full_name = value.strip()
    owner, repo = _parse_github_remote(f"https://github.com/{repo_full_name}")
    if owner is None or repo is None or repo_full_name.count("/") != 1:
        raise RebaseWorkflowError("GitHub repository must use the owner/name format")
    return f"{owner}/{repo}"


def _prompt_existing_repo(args: Any) -> str:
    repo_full_name = _prompt(args.repo, "GitHub repository full name", default=_local_github_remote())
    return _validate_github_repo_full_name(repo_full_name)


def _await_authorize_code(
    server: _CallbackServer,
    *,
    auth_url: str,
    no_browser: bool,
    auth_timeout: float,
) -> str:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    if no_browser:
        _hint("Open this URL to authenticate:")
        print(auth_url)
    else:
        webbrowser.open(auth_url)
        _hint("Opened browser for Supabase login")
    deadline = time.monotonic() + auth_timeout
    while thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.1)
    server.shutdown()
    thread.join(timeout=2)
    params = server.callback_params
    if params is None:
        raise RebaseWorkflowError("timed out waiting for Supabase callback")
    if params.get("error"):
        raise RebaseWorkflowError(params.get("error_description") or params["error"])
    code = params.get("code")
    if not code:
        raise RebaseWorkflowError("Supabase callback did not include code")
    return code


def run_link_identity(args: Any) -> None:
    """Attach another social login to the account already signed in on this machine.

    This is how someone who first onboarded with Google connects their GitHub
    account afterwards: signing in with GitHub from scratch would mint a separate
    Supabase user (unless the emails happen to match), while this flow adds the
    identity to the existing user, so the profile keeps its memberships and gains
    the GitHub username.
    """
    provider = args.provider
    if provider not in {"google", "github"}:
        raise RebaseWorkflowError("provider must be 'google' or 'github'")
    try:
        session = load_session()
    except AuthError as exc:
        raise RebaseWorkflowError(str(exc)) from exc
    if session is None:
        raise RebaseWorkflowError("no stored login session. Run `rebase setup` to sign in first")
    if session.is_expired:
        try:
            session = refresh_session(session)
        except AuthError as exc:
            raise RebaseWorkflowError(
                f"the stored session could not be refreshed ({exc}). Run `rebase setup --force-auth` and try again"
            ) from exc

    supabase_url = session.supabase_url
    supabase_anon_key = session.supabase_anon_key
    if not supabase_url or not supabase_anon_key:
        config = Client(api_url=getattr(args, "api_url", None)).setup_config()
        supabase_url = supabase_url or config.get("supabase_url")
        supabase_anon_key = supabase_anon_key or config.get("supabase_anon_key")
    if not isinstance(supabase_url, str) or not isinstance(supabase_anon_key, str):
        raise RebaseWorkflowError("could not determine the Supabase URL and anon key for the link flow")

    # Completing the browser round-trip for an identity that is already attached
    # ends in GoTrue's "Identity is already linked" error, so look before leaping.
    try:
        identities = fetch_user_identities(
            supabase_url=supabase_url,
            supabase_anon_key=supabase_anon_key,
            access_token=session.access_token,
        )
    except AuthError:
        identities = []
    for identity in identities:
        if identity.get("provider") != provider:
            continue
        identity_data = identity.get("identity_data")
        login = identity_data.get("user_name") if isinstance(identity_data, dict) else None
        suffix = f" (@{login})" if isinstance(login, str) and login else ""
        _success(f"{provider} is already linked to {session.email or 'your account'}{suffix} — nothing to do")
        _sync_profile_after_link(args, provider=provider, access_token=session.access_token)
        return

    verifier = generate_pkce_verifier()
    server = _CallbackServer(("127.0.0.1", args.callback_port), _CallbackHandler)
    redirect_to = f"http://127.0.0.1:{server.server_port}/auth/callback"
    try:
        link_url = fetch_link_identity_url(
            supabase_url=supabase_url,
            supabase_anon_key=supabase_anon_key,
            access_token=session.access_token,
            provider=provider,
            redirect_to=redirect_to,
            code_challenge=pkce_challenge(verifier),
        )
    except AuthError as exc:
        server.server_close()
        raise RebaseWorkflowError(str(exc)) from exc
    code = _await_authorize_code(
        server,
        auth_url=link_url,
        no_browser=args.no_browser,
        auth_timeout=args.auth_timeout,
    )
    try:
        linked = exchange_pkce_code(
            supabase_url=supabase_url,
            supabase_anon_key=supabase_anon_key,
            auth_code=code,
            code_verifier=verifier,
        )
    except AuthError as exc:
        raise RebaseWorkflowError(str(exc)) from exc
    if session.user_id and linked.user_id and linked.user_id != session.user_id:
        raise RebaseWorkflowError(
            f"the link flow signed in a different user ({linked.email or linked.user_id}) instead of "
            f"linking to {session.email or session.user_id}; the stored session was left untouched"
        )
    save_session(linked)
    _success(f"Linked {provider} to {linked.email or 'your account'}")
    _sync_profile_after_link(args, provider=provider, access_token=linked.access_token)


def _sync_profile_after_link(args: Any, *, provider: str, access_token: str) -> None:
    # An authenticated request makes the backend re-read the token: it fills in
    # profiles.github_username from the fresh claims and accepts any pending
    # invites that name the newly linked identity.
    profile: dict[str, Any] | None = None
    try:
        profile = Client(api_url=getattr(args, "api_url", None)).get_my_profile()
    except Exception as exc:  # noqa: BLE001 - linking already succeeded; the sync retries on any later request
        _hint(f"Could not refresh your Rebase profile right away ({exc}); it will sync on your next command.")
    if provider == "github":
        github_username = profile.get("github_username") if profile else None
        if github_username:
            _success(f"Your Rebase profile carries GitHub username @{github_username}")
        elif github_username_from_jwt(access_token) is None:
            _hint(
                "GitHub is linked, but the session does not carry your GitHub username yet. "
                "Sign in once with GitHub — `rebase setup --force-auth --provider github` — to finish the sync."
            )


def _oauth_session(args: Any, config: dict[str, Any]) -> str:
    supabase_url = config.get("supabase_url")
    supabase_anon_key = config.get("supabase_anon_key")
    if not isinstance(supabase_url, str) or not isinstance(supabase_anon_key, str):
        raise RebaseWorkflowError("workflow API did not provide Supabase URL and anon key")

    provider = args.provider or _choose("auth provider", ["google", "github"], default="google")
    if provider not in {"google", "github"}:
        raise RebaseWorkflowError("provider must be 'google' or 'github'")

    verifier = generate_pkce_verifier()
    server = _CallbackServer(("127.0.0.1", args.callback_port), _CallbackHandler)
    redirect_to = f"http://127.0.0.1:{server.server_port}/auth/callback"
    auth_url = build_supabase_authorize_url(
        supabase_url=supabase_url,
        provider=provider,
        redirect_to=redirect_to,
        code_challenge=pkce_challenge(verifier),
    )
    code = _await_authorize_code(server, auth_url=auth_url, no_browser=args.no_browser, auth_timeout=args.auth_timeout)
    try:
        session = exchange_pkce_code(
            supabase_url=supabase_url,
            supabase_anon_key=supabase_anon_key,
            auth_code=code,
            code_verifier=verifier,
        )
    except AuthError as exc:
        raise RebaseWorkflowError(str(exc)) from exc
    save_session(session)
    return session.access_token


def _reauth_args(args: Any) -> Any:
    """`args` as they should be for signing in *again*.

    The provider is cleared so the picker comes back: someone switching accounts is
    usually switching provider too, and a `--provider` from the first attempt would
    silently pin the retry to the login that just failed them.
    """
    reauth = copy.copy(args)
    reauth.provider = None
    reauth.force_auth = True
    return reauth


def _switch_account(args: Any, config: dict[str, Any]) -> str:
    """Drop the stored session and authenticate from scratch."""
    try:
        clear_session()
    except OSError as exc:
        raise RebaseWorkflowError(f"could not clear the stored session: {exc}") from exc
    return _oauth_session(_reauth_args(args), config)


def _stored_session() -> Any | None:
    try:
        return load_session()
    except AuthError:
        return None


def _session_is_for_another_identity_provider(session: Any | None, config: dict[str, Any]) -> tuple[str, str] | None:
    """The stored session's issuer and this API's, when they are not the same project.

    A session is minted by one Supabase project and means nothing to another. Offering
    it as "Continue as you" would sign in with a token the API cannot verify -- and
    refreshing it first would spend a round trip on the wrong project to get there.
    """
    stored = getattr(session, "supabase_url", None)
    expected = config.get("supabase_url")
    if not isinstance(stored, str) or not stored or not isinstance(expected, str) or not expected:
        return None
    if stored.rstrip("/") == expected.rstrip("/"):
        return None
    return stored.rstrip("/"), expected.rstrip("/")


def _access_token(args: Any, config: dict[str, Any]) -> str:
    if not args.force_auth:
        mismatch = _session_is_for_another_identity_provider(_stored_session(), config)
        if mismatch is not None:
            stored, expected = mismatch
            _hint(
                f"The stored session was issued by {stored}, but this API signs in through {expected}; "
                "signing in afresh."
            )
            return _oauth_session(args, config)
        try:
            token = load_access_token()
        except AuthError as exc:
            raise RebaseWorkflowError(str(exc)) from exc
        if token:
            return _confirm_stored_account(args, config, token)
    return _oauth_session(args, config)


def _confirm_stored_account(args: Any, config: dict[str, Any], token: str) -> str:
    """Offer the stored session as a choice rather than assuming it.

    A rerun of `rebase setup` is nearly always a rerun because something went wrong,
    and the most common something is being signed in as the wrong identity — a
    GitHub login whose email is not the address the invite went to. The stored token
    used to be returned in silence, which put the provider picker out of reach for
    good and made every rerun fail the same way.
    """
    if not _can_prompt():
        return token
    who = _session_description(_stored_session())
    keep = f"Continue as {who}"
    selected = _choose(
        "account",
        [keep, SWITCH_ACCOUNT],
        default=keep,
        title="You are already signed in on this computer.",
    )
    if selected == keep:
        return token
    return _switch_account(args, config)


def _workspace_by_id(workspaces: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(workspace["id"]): workspace for workspace in workspaces}


def _signed_in_as_suffix(session: Any | None) -> str:
    """The identity behind a permission failure, spelled out.

    An invite is granted to an address, so "you were not invited" is only actionable
    next to the address you are actually signed in as — the two differ exactly when
    this message appears.
    """
    if session is None:
        return ""
    return f" You are signed in as {_session_description(session)}."


def _no_workspace_access_message(workspace_id: str, *, session: Any | None) -> str:
    return (
        f"You do not have access to workspace {workspace_id!r}."
        f"{_signed_in_as_suffix(session)}"
        " Ask an owner to invite you, or — if the invite went to a different address — "
        "sign in with a different account (`rebase setup --force-auth`)."
    )


def _select_joined_workspace(
    workspace_id: str,
    workspaces_by_id: dict[str, dict[str, Any]],
    *,
    session: Any | None = None,
) -> dict[str, Any]:
    workspace = workspaces_by_id.get(workspace_id)
    if workspace is None:
        raise RebaseWorkflowError(_no_workspace_access_message(workspace_id, session=session))
    return workspace


def _workspace_display_name(workspace: dict[str, Any]) -> str:
    workspace_id = str(workspace["id"])
    name = workspace.get("name")
    return name if isinstance(name, str) and name else workspace_id


def _is_workspace_creation_permission_error(error: RebaseWorkflowError) -> bool:
    message = str(error)
    return any(
        needle in message
        for needle in (
            "creating a workspace requires a platform beta invite",
            "creating a workspace requires a beta invite",
            "workspace creation limit reached",
        )
    )


def _workspace_creation_error(error: RebaseWorkflowError, *, session: Any | None = None) -> RebaseWorkflowError:
    if "workspace creation limit reached" in str(error) or "You've reached your quota" in str(error):
        return RebaseWorkflowError(WORKSPACE_CREATION_QUOTA_ERROR)
    if _is_workspace_creation_permission_error(error):
        return RebaseWorkflowError(f"{BETA_ENROLLMENT_ERROR}{_signed_in_as_suffix(session)}")
    return error


def _create_workspace(args: Any, client: Client, *, session: Any | None) -> dict[str, Any]:
    profile = _ensure_profile_handle(args, client, session=session)
    profile_handle = profile.get("handle")
    fallback_workspace = profile_handle if isinstance(profile_handle, str) and profile_handle else None
    default_workspace = _directory_handle_suggestion() or fallback_workspace
    requested_workspace = getattr(args, "workspace", None)
    workspace_id = _normalize_handle(
        _prompt(requested_workspace, "Workspace handle to create", default=default_workspace),
        label="Workspace handle",
    )
    try:
        return client.create_workspace(workspace_id, name=args.workspace_name)
    except RebaseWorkflowError as exc:
        raise _workspace_creation_error(exc, session=session) from exc


def _select_invited_workspace(workspaces: list[dict[str, Any]]) -> dict[str, Any] | str | None:
    invited_workspaces = [workspace for workspace in workspaces if workspace.get("joined_via_invite")]
    if not invited_workspaces:
        return None
    workspace_options = [f"Join workspace ({_workspace_display_name(workspace)})" for workspace in invited_workspaces]
    selected = _choose(
        "workspace setup",
        [*workspace_options, CREATE_WORKSPACE, SWITCH_ACCOUNT],
        default=workspace_options[0],
        title="You were invited to a workspace. What do you want to do?",
    )
    if selected == SWITCH_ACCOUNT:
        raise _SwitchAccountRequested
    if selected == CREATE_WORKSPACE:
        return CREATE_WORKSPACE
    selected_index = workspace_options.index(selected)
    return invited_workspaces[selected_index]


def _join_workspace(
    args: Any,
    client: Client,
    workspaces_by_id: dict[str, dict[str, Any]],
    *,
    session: Any | None,
) -> dict[str, Any]:
    """Ask which workspace to join, and keep the flow alive when there is no access.

    A handle you were not invited to used to end the run, which is the worst moment
    to end it: the fix is nearly always signing in as the address the invite was sent
    to, and quitting is what made that unreachable on the next run.
    """
    while True:
        workspace_id = _normalize_handle(_prompt(None, "Workspace handle to join"), label="Workspace handle")
        workspace = workspaces_by_id.get(workspace_id)
        if workspace is not None:
            return workspace
        if not _can_prompt():
            return _select_joined_workspace(workspace_id, workspaces_by_id, session=session)
        _failure(f"You do not have access to workspace {workspace_id!r}.{_signed_in_as_suffix(session)}")
        _hint("An invite is granted to one address. If yours went elsewhere, sign in with that account.")
        selected = _choose(
            "next step",
            [TRY_ANOTHER_HANDLE, SWITCH_ACCOUNT, CREATE_WORKSPACE],
            default=TRY_ANOTHER_HANDLE,
            title="What do you want to do?",
        )
        if selected == SWITCH_ACCOUNT:
            raise _SwitchAccountRequested
        if selected == CREATE_WORKSPACE:
            return _create_workspace(args, client, session=session)


def _select_workspace(args: Any, client: Client, *, session: Any | None) -> dict[str, Any]:
    workspaces = client.list_my_workspaces()
    workspaces_by_id = _workspace_by_id(workspaces)
    if args.workspace:
        workspace_id = _normalize_handle(args.workspace, label="Workspace handle")
        if workspace_id not in workspaces_by_id:
            _ensure_profile_handle(args, client, session=session)
            try:
                return client.create_workspace(workspace_id, name=args.workspace_name or workspace_id)
            except RebaseWorkflowError as exc:
                raise _workspace_creation_error(exc, session=session) from exc
        return workspaces_by_id[workspace_id]
    invited_workspace = _select_invited_workspace(workspaces)
    if invited_workspace == CREATE_WORKSPACE:
        return _create_workspace(args, client, session=session)
    if invited_workspace is not None:
        return invited_workspace
    action = _choose(
        "workspace setup",
        [JOIN_WORKSPACE, CREATE_WORKSPACE, SWITCH_ACCOUNT],
        default=JOIN_WORKSPACE if workspaces else CREATE_WORKSPACE,
        title="Do you want to join an existing workspace or create a new one?",
    )
    if action == SWITCH_ACCOUNT:
        raise _SwitchAccountRequested
    if action == JOIN_WORKSPACE:
        return _join_workspace(args, client, workspaces_by_id, session=session)
    return _create_workspace(args, client, session=session)


def _start_github_installation(args: Any, client: Client, *, workspace_id: str) -> dict[str, Any]:
    setup_session = client.create_github_setup_session(workspace_id=workspace_id)
    install_url = setup_session["install_url"]
    if args.no_browser:
        _hint("Open this URL to install the Rebase GitHub App:")
        print(install_url)
    else:
        webbrowser.open(install_url)
        _hint("Opened browser for GitHub App installation")
    return setup_session


def _select_repo(
    args: Any,
    client: Client,
    installation_id: int,
    *,
    repo_full_name: str | None = None,
) -> dict[str, Any]:
    repos = client.list_github_repositories(installation_id)
    if not repos:
        raise RebaseWorkflowError("GitHub App installation has no accessible repositories")
    by_full_name = {repo["full_name"].lower(): repo for repo in repos}
    requested_repo = repo_full_name or args.repo
    if requested_repo:
        repo = by_full_name.get(requested_repo.lower())
        if repo is None:
            raise RebaseWorkflowError(
                "repository is not accessible through this installation. "
                f"Make sure you selected {requested_repo} when installing the Rebase GitHub App."
            )
        return repo
    local_remote = _local_github_remote()
    default = local_remote if local_remote and local_remote.lower() in by_full_name else repos[0]["full_name"]
    selected = _choose("repository", [repo["full_name"] for repo in repos], default=default)
    return by_full_name[selected.lower()]


def _find_repo_installation(client: Client, repo_full_name: str) -> tuple[int, dict[str, Any]]:
    response = client.find_github_repository_installation(repo_full_name)
    installation_id = response.get("installation_id")
    repo = response.get("repository")
    if not isinstance(installation_id, int) or not isinstance(repo, dict):
        raise RebaseWorkflowError("expected GitHub repository installation response")
    return installation_id, repo


def _repo_name_seed(*values: str | None) -> str:
    for value in values:
        if value:
            seed = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower()).strip("-._")
            if seed:
                return seed[:100]
    return "rebase-workflows"


def _should_create_repo(args: Any) -> bool:
    if args.create_repo:
        return True
    if args.repo:
        return False
    selected = _choose(
        "repository setup",
        ["Select an existing repository", "Create a new one"],
        default="Create a new one",
        title="Do you want to use an existing GitHub repository as your sync repo or create a new one?",
    )
    return selected == "Create a new one"


def _create_repo_in_browser(args: Any, *, default_name: str) -> str:
    new_repo_url = f"https://github.com/new?{urlencode({'name': default_name})}"
    if args.no_browser:
        _hint("Open this URL to create the repository:")
        print(new_repo_url)
    else:
        webbrowser.open(new_repo_url)
        _hint("Opened browser for GitHub repository creation")
    _hint("Create the repository in GitHub, then return here.")
    return _prompt(args.repo, "GitHub repository full name")


def _select_project(args: Any, client: Client) -> dict[str, Any]:
    if args.project:
        return client.ensure_project(args.project)
    projects = client.list_projects()
    if projects:
        selected = _choose("project", [project["name"] for project in projects], default=projects[0]["name"])
        return client.ensure_project(selected)
    project_name = _prompt(None, "Project name", default="default")
    return client.ensure_project(project_name)


def _select_repo_scope(args: Any) -> str:
    if args.repo_scope:
        return args.repo_scope
    return _choose("GitHub connection scope", ["workspace", "project"], default="workspace")


def _connect_github(args: Any, client: Client, *, workspace_id: str) -> dict[str, Any]:
    setup_status = {"installation_id": args.github_installation_id} if args.github_installation_id else None
    repo = None
    scope = _select_repo_scope(args)
    project_id = None
    project_name = None
    if scope == "project":
        project = _select_project(args, client)
        project_id = project["id"]
        project_name = project["name"]
    elif scope != "workspace":
        raise RebaseWorkflowError("repo scope must be 'workspace' or 'project'")
    repo_full_name = None
    if _should_create_repo(args):
        repo_full_name = _create_repo_in_browser(
            args,
            default_name=_repo_name_seed(project_name, workspace_id),
        )
        _hint("Select that repository when GitHub asks which repositories the Rebase App can access.")
    else:
        repo_full_name = _prompt_existing_repo(args)
        _hint(f"Verifying the Rebase GitHub App can access {repo_full_name}.")
    if setup_status is None:
        _hint(f"Select {repo_full_name} when GitHub asks which repositories the Rebase App can access.")
        _start_github_installation(args, client, workspace_id=workspace_id)
        try:
            installation_id, repo = _find_repo_installation(client, repo_full_name)
        except RebaseWorkflowError as exc:
            _hint("GitHub App access is not visible yet.")
            _hint("Finish selecting this repository in GitHub, then return to this terminal.")
            _read_input("Press Enter to verify GitHub access again: ")
            try:
                installation_id, repo = _find_repo_installation(client, repo_full_name)
            except RebaseWorkflowError:
                _failure(f"Could not verify GitHub App access to {repo_full_name}")
                raise RebaseWorkflowError(
                    f"GitHub App cannot access {repo_full_name}. "
                    "Install or configure the Rebase GitHub App for this repository, then run rebase connect github."
                ) from exc
        setup_status = {"installation_id": installation_id}
    installation_id = int(setup_status["installation_id"])
    try:
        if repo is None:
            repo = _select_repo(args, client, installation_id, repo_full_name=repo_full_name)
    except RebaseWorkflowError:
        if repo_full_name:
            _failure(f"Could not verify GitHub App access to {repo_full_name}")
        raise
    if repo_full_name:
        _success(f"Verified GitHub App access to {repo_full_name}")
    connection = client.connect_github_repo(
        scope=scope,
        installation_id=installation_id,
        repo_id=repo["id"],
        repo_owner=repo["owner"],
        repo_name=repo["name"],
        repo_path=args.repo_path,
        default_branch=repo.get("default_branch"),
        project_id=project_id,
    )
    _success(f"Connected {connection['repo_owner']}/{connection['repo_name']} at {scope} level")
    return connection


def _repo_full_name_from_connection(connection: dict[str, Any]) -> str:
    repo_owner = connection.get("repo_owner")
    repo_name = connection.get("repo_name")
    if not isinstance(repo_owner, str) or not repo_owner or not isinstance(repo_name, str) or not repo_name:
        raise RebaseWorkflowError("GitHub repo connection response is missing repo_owner or repo_name")
    return f"{repo_owner}/{repo_name}"


def _repo_full_name_from_workspace(workspace: dict[str, Any]) -> str | None:
    repo_owner = workspace.get("repo_owner")
    repo_name = workspace.get("repo_name")
    if isinstance(repo_owner, str) and repo_owner and isinstance(repo_name, str) and repo_name:
        return f"{repo_owner}/{repo_name}"
    return None


def _workspace_repo_full_name(client: Client) -> str | None:
    get_workspace = getattr(client, "get_workspace", None)
    if not callable(get_workspace):
        return None
    workspace = get_workspace()
    if not isinstance(workspace, dict):
        raise RebaseWorkflowError("expected workspace response")
    return _repo_full_name_from_workspace(workspace)


def _workspace_github_connection(
    client: Client,
    *,
    repo_full_name: str | None = None,
) -> dict[str, Any] | None:
    connections = client.list_github_repo_connections()
    workspace_connections = [
        connection
        for connection in connections
        if connection.get("scope") == "workspace" and connection.get("project_id") is None
    ]
    if repo_full_name:
        expected = repo_full_name.lower()
        for connection in reversed(workspace_connections):
            if _repo_full_name_from_connection(connection).lower() == expected:
                return connection
        return None
    return workspace_connections[-1] if workspace_connections else None


def _ssh_clone_url(repo_full_name: str) -> str:
    return f"git@github.com:{repo_full_name}.git"


def _https_clone_url(repo_full_name: str) -> str:
    return f"https://github.com/{repo_full_name}.git"


def _origin_url(cwd: Path) -> str | None:
    return _git(["config", "--get", "remote.origin.url"], cwd=cwd)


def _remote_protocol(origin_url: str | None) -> str | None:
    if origin_url is None:
        return None
    if origin_url.startswith("git@github.com:"):
        return "SSH"
    if origin_url.startswith("https://github.com/"):
        return "HTTPS"
    return None


def _select_origin_url(repo_full_name: str, *, default_protocol: str = "SSH") -> str:
    options = {
        "SSH": _ssh_clone_url(repo_full_name),
        "HTTPS": _https_clone_url(repo_full_name),
    }
    labels = [f"{protocol} ({url})" for protocol, url in options.items()]
    default_label = f"{default_protocol} ({options[default_protocol]})"
    selected = _choose("GitHub transport", labels, default=default_label, title="How should Git connect to GitHub?")
    protocol = selected.split(" ", 1)[0]
    return options[protocol]


def _ensure_workspace_origin_transport(cwd: Path, repo_full_name: str, *, prompt: bool = False) -> None:
    origin_url = _origin_url(cwd)
    owner, name = _parse_github_remote(origin_url)
    if origin_url is None or owner is None or name is None:
        return
    if f"{owner}/{name}".lower() != repo_full_name.lower():
        return

    current_protocol = _remote_protocol(origin_url)
    if current_protocol is None:
        return
    if not prompt:
        return
    selected_url = _select_origin_url(repo_full_name, default_protocol=current_protocol)
    if selected_url != origin_url:
        _run_git(
            ["remote", "set-url", "origin", selected_url],
            cwd=cwd,
            action="update GitHub origin",
        )
        _hint(f"Updated Git origin to use {_remote_protocol(selected_url)}.")


def _directory_listing_for_prompt(path: Path) -> str:
    names = sorted(entry.name for entry in path.iterdir())
    if not names:
        return "empty"
    shown = ", ".join(names[:5])
    if len(names) > 5:
        shown += f", and {len(names) - 5} more"
    return shown


def _detect_remote_default_branch(cwd: Path) -> str:
    remote_head = _git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=cwd)
    if remote_head and remote_head.startswith("origin/"):
        return remote_head.split("/", 1)[1]
    for branch in _remote_branches(cwd):
        if branch.startswith("origin/"):
            return branch.split("/", 1)[1]
    return "main"


def _remote_branches(cwd: Path) -> list[str]:
    branches = _git(["branch", "-r", "--format=%(refname:short)"], cwd=cwd)
    if not branches:
        return []
    return [
        branch
        for branch in branches.splitlines()
        if branch and branch != "origin/HEAD" and branch.startswith("origin/")
    ]


def _remote_branch_exists(cwd: Path, branch: str) -> bool:
    return _git(["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"], cwd=cwd) is not None


def _has_local_head(cwd: Path) -> bool:
    return _git(["rev-parse", "--verify", "HEAD"], cwd=cwd) is not None


def _checkout_remote_branch(cwd: Path, *, default_branch: str | None) -> None:
    branch = default_branch or _detect_remote_default_branch(cwd)
    if _remote_branch_exists(cwd, branch):
        _run_git(
            ["checkout", "-B", branch, f"origin/{branch}"],
            cwd=cwd,
            action=f"check out origin/{branch}",
            timeout=60,
        )
        return

    detected_branch = _detect_remote_default_branch(cwd)
    if detected_branch != branch and _remote_branch_exists(cwd, detected_branch):
        _run_git(
            ["checkout", "-B", detected_branch, f"origin/{detected_branch}"],
            cwd=cwd,
            action=f"check out origin/{detected_branch}",
            timeout=60,
        )
        return

    _run_git(
        ["checkout", "-B", branch],
        cwd=cwd,
        action=f"create local branch {branch}",
        timeout=60,
    )


def _seed_empty_workspace_repo(client: Client | None, connection: dict[str, Any], *, cwd: Path) -> None:
    if _remote_branches(cwd):
        return
    connection_id = connection.get("id")
    if client is None or connection_id is None:
        return

    _hint("GitHub repository has no branches yet. Creating a starter workflow on the default branch.")
    try:
        starter = client.create_github_starter_workflow(str(connection_id))
    except RebaseWorkflowError as exc:
        raise RebaseWorkflowError(
            "GitHub repository is empty and Rebase could not create a starter workflow. "
            "Create an initial commit in the repository, then run `rebase connect github` again."
        ) from exc
    path = starter.get("path")
    commit_sha = starter.get("commit_sha")
    suffix = f" ({commit_sha})" if isinstance(commit_sha, str) and commit_sha else ""
    if isinstance(path, str) and path:
        _success(f"Created starter workflow at {path}{suffix}")
    else:
        _success("Created starter workflow")
    _run_git(["fetch", "origin"], cwd=cwd, action="fetch starter workflow", timeout=300)


def _clone_workspace_repo_into_current_directory(
    connection: dict[str, Any],
    *,
    client: Client | None,
) -> None:
    repo_full_name = _repo_full_name_from_connection(connection)
    cwd = Path.cwd()
    contents = _directory_listing_for_prompt(cwd)
    if not _confirm(
        f"Current folder is not a git repository and contains: {contents}. Clone {repo_full_name} into this folder?",
        default=True,
    ):
        raise RebaseWorkflowError(
            "current folder is not a git repository. "
            f"Run this command inside {repo_full_name}, or rerun it and choose Yes to clone the workspace repo."
        )

    _hint(f"Initializing current folder as {repo_full_name}. Existing local files are kept.")
    _run_git(["init"], cwd=cwd, action="initialize a git repository")
    if _origin_url(cwd) is None:
        _run_git(
            ["remote", "add", "origin", _select_origin_url(repo_full_name)],
            cwd=cwd,
            action="add GitHub origin",
        )
    else:
        _ensure_workspace_origin_transport(cwd, repo_full_name, prompt=True)
    _run_git(["fetch", "origin"], cwd=cwd, action=f"fetch {repo_full_name}", timeout=300)
    _seed_empty_workspace_repo(client, connection, cwd=cwd)
    _checkout_remote_branch(cwd, default_branch=_connection_default_branch(connection))
    _hint(f"Cloned {repo_full_name} into the current folder.")


def _repair_empty_local_workspace_repo(client: Client | None, connection: dict[str, Any], *, cwd: Path) -> None:
    if _has_local_head(cwd):
        return
    repo_full_name = _repo_full_name_from_connection(connection)
    _run_git(["fetch", "origin"], cwd=cwd, action=f"fetch {repo_full_name}", timeout=300)
    _seed_empty_workspace_repo(client, connection, cwd=cwd)
    _checkout_remote_branch(cwd, default_branch=_connection_default_branch(connection))


def _connection_default_branch(connection: dict[str, Any]) -> str | None:
    default_branch = connection.get("default_branch")
    return default_branch if isinstance(default_branch, str) and default_branch else None


def _ensure_local_workspace_repo(connection: dict[str, Any], *, client: Client | None = None) -> None:
    repo_full_name = _repo_full_name_from_connection(connection)
    git_root = _current_git_root()
    if git_root is None:
        _clone_workspace_repo_into_current_directory(connection, client=client)
        git_root = _current_git_root()
        if git_root is None:
            raise RebaseWorkflowError(f"cloned {repo_full_name}, but could not find a git repository")
        _success(f"Current folder is inside {repo_full_name}")
        return

    local_repo = _local_github_remote(git_root)
    if local_repo is None:
        raise RebaseWorkflowError(
            "you are not in the same GitHub repo as your Rebase workspace. "
            f"The active workspace is connected to {repo_full_name}, but this git repository has no GitHub origin."
        )
    if local_repo.lower() != repo_full_name.lower():
        raise RebaseWorkflowError(
            "you are not in the same GitHub repo as your Rebase workspace. "
            f"The active workspace is connected to {repo_full_name}, but this folder uses {local_repo}."
        )
    _ensure_workspace_origin_transport(git_root, repo_full_name)
    _repair_empty_local_workspace_repo(client, connection, cwd=git_root)
    _success(f"Current folder is inside {repo_full_name}")


def _verify_github_app_access(args: Any, client: Client, connection: dict[str, Any], *, workspace_id: str) -> None:
    repo_full_name = _repo_full_name_from_connection(connection)
    try:
        _find_repo_installation(client, repo_full_name)
    except RebaseWorkflowError:
        _hint(f"The Rebase GitHub App is not visible for {repo_full_name}.")
        _hint(f"Select {repo_full_name} when GitHub asks which repositories the Rebase App can access.")
        _start_github_installation(args, client, workspace_id=workspace_id)
        _read_input("Press Enter to verify GitHub App access again: ")
        try:
            _find_repo_installation(client, repo_full_name)
        except RebaseWorkflowError as retry_exc:
            raise RebaseWorkflowError(
                f"GitHub App cannot access {repo_full_name}. "
                "Install or configure the Rebase GitHub App for this repository, then run rebase connect github."
            ) from retry_exc
        _success(f"Verified Rebase GitHub App access to {repo_full_name}")
        return
    _success(f"Verified Rebase GitHub App access to {repo_full_name}")


def _workspace_github_connect_args(args: Any, *, repo: str | None = None) -> Any:
    return SimpleNamespace(
        profile=args.profile,
        api_url=args.api_url,
        no_browser=args.no_browser,
        github_installation_id=args.github_installation_id,
        github_timeout=args.github_timeout,
        poll_interval=args.poll_interval,
        repo=repo if repo is not None else args.repo,
        repo_scope="workspace",
        repo_path=args.repo_path,
        create_repo=args.create_repo if repo is None else False,
        project=None,
    )


def _huggingface_error(response: requests.Response, *, default: str) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text or default
    if isinstance(payload, dict):
        description = payload.get("error_description")
        if isinstance(description, str) and description:
            return description
        error = payload.get("error")
        if isinstance(error, str) and error:
            return error
    return default


def _huggingface_scope_string(scopes: list[str] | None) -> str:
    selected = scopes or list(HUGGINGFACE_DEFAULT_SCOPES)
    cleaned: list[str] = []
    for scope_value in selected:
        for scope in scope_value.split():
            if scope and scope not in cleaned:
                cleaned.append(scope)
    if not cleaned:
        raise RebaseWorkflowError("at least one Hugging Face OAuth scope is required")
    return " ".join(cleaned)


def _resolve_huggingface_client_id(args: Any) -> str:
    configured = getattr(args, "client_id", None) or os.getenv(HUGGINGFACE_CLIENT_ID_ENV)
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    try:
        config = Client(api_url=getattr(args, "api_url", None), profile=getattr(args, "profile", None)).setup_config()
    except RebaseWorkflowError:
        config = {}
    value = config.get("huggingface_oauth_client_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise RebaseWorkflowError(
        f"Hugging Face OAuth client id is not configured. Set {HUGGINGFACE_CLIENT_ID_ENV} or pass --client-id."
    )


def _create_huggingface_device_code(*, client_id: str, scope: str) -> dict[str, Any]:
    try:
        response = requests.post(
            HUGGINGFACE_DEVICE_URL,
            data={"client_id": client_id, "scope": scope},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise RebaseWorkflowError(f"could not start Hugging Face OAuth device flow: {exc}") from exc
    if response.status_code >= 400:
        raise RebaseWorkflowError(
            _huggingface_error(response, default="could not start Hugging Face OAuth device flow")
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RebaseWorkflowError("expected Hugging Face device-code response to be JSON") from exc
    if not isinstance(payload, dict):
        raise RebaseWorkflowError("expected Hugging Face device-code response")
    return payload


def _start_huggingface_device_flow(args: Any, *, client_id: str, scope: str) -> dict[str, Any]:
    device = _create_huggingface_device_code(client_id=client_id, scope=scope)
    device_code = device.get("device_code")
    verification_uri = device.get("verification_uri_complete") or device.get("verification_uri")
    if not isinstance(device_code, str) or not device_code:
        raise RebaseWorkflowError("Hugging Face device-code response is missing device_code")
    if not isinstance(verification_uri, str) or not verification_uri:
        raise RebaseWorkflowError("Hugging Face device-code response is missing verification_uri")
    if getattr(args, "no_browser", False):
        _hint("Open this URL to authorize Rebase with Hugging Face:")
        print(verification_uri)
    else:
        webbrowser.open(verification_uri)
        _hint("Opened browser for Hugging Face authorization")
    user_code = device.get("user_code")
    if isinstance(user_code, str) and user_code:
        _hint("Enter this code in Hugging Face if prompted:")
        print(user_code)
    _hint("Waiting for Hugging Face authorization...")
    return device


def _wait_for_huggingface_token(args: Any, *, client_id: str, device: dict[str, Any]) -> str:
    device_code = device["device_code"]
    device_interval = device.get("interval")
    interval = float(getattr(args, "poll_interval", None) or device_interval or 5)
    expires_in = device.get("expires_in")
    timeout = float(getattr(args, "timeout", None) or expires_in or 900)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = requests.post(
                HUGGINGFACE_TOKEN_URL,
                data={
                    "grant_type": HUGGINGFACE_DEVICE_GRANT_TYPE,
                    "device_code": device_code,
                    "client_id": client_id,
                },
                timeout=30,
            )
        except requests.RequestException as exc:
            raise RebaseWorkflowError(f"could not poll Hugging Face OAuth token: {exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise RebaseWorkflowError("expected Hugging Face token response to be JSON") from exc
        if response.status_code < 400:
            if isinstance(payload, dict) and isinstance(payload.get("access_token"), str):
                return payload["access_token"]
            raise RebaseWorkflowError("Hugging Face token response is missing access_token")
        error = payload.get("error") if isinstance(payload, dict) else None
        if error == "authorization_pending":
            time.sleep(interval)
            continue
        if error == "slow_down":
            interval += 5
            time.sleep(interval)
            continue
        if error == "expired_token":
            raise RebaseWorkflowError("Hugging Face authorization expired")
        if error == "access_denied":
            raise RebaseWorkflowError("Hugging Face authorization was denied")
        raise RebaseWorkflowError(
            _huggingface_error(response, default="could not complete Hugging Face OAuth device flow")
        )
    raise RebaseWorkflowError("timed out waiting for Hugging Face authorization")


def _save_huggingface_token(token: str, *, add_to_git_credential: bool) -> None:
    login = _huggingface_login_function()
    login(token=token, add_to_git_credential=add_to_git_credential, skip_if_logged_in=False)


def _huggingface_login_function() -> Any:
    try:
        from huggingface_hub import login
    except ImportError as exc:
        raise RebaseWorkflowError(
            "Install the Hugging Face extra before connecting: uv add 'rebase-toolkit[huggingface]' "
            "or pip install 'rebase-toolkit[huggingface]'."
        ) from exc
    return login


def run_connect_huggingface(args: Any) -> int:
    _restore_terminal_for_prompts()
    _section("Hugging Face")
    _huggingface_login_function()
    client_id = _resolve_huggingface_client_id(args)
    scope = _huggingface_scope_string(getattr(args, "scope", None))
    device = _start_huggingface_device_flow(args, client_id=client_id, scope=scope)
    token = _wait_for_huggingface_token(args, client_id=client_id, device=device)
    _save_huggingface_token(
        token,
        add_to_git_credential=bool(getattr(args, "add_to_git_credential", False)),
    )
    _success("Hugging Face token saved")
    return 0


def run_setup(args: Any) -> int:
    _restore_terminal_for_prompts()
    _intro()
    client = Client(api_url=args.api_url, profile=args.profile)
    config = client.setup_config()
    total_steps = len(SETUP_STEPS)
    _section("Authenticate", step=1, total=total_steps)
    token = _access_token(args, config)
    while True:
        authed_client = Client(api_url=client.api_url, access_token=token, profile=args.profile)
        session = load_session()
        if session is not None:
            _success(f"Authenticated as {_session_description(session)}")
        _section("Workspace", step=2, total=total_steps)
        try:
            workspace = _select_workspace(args, authed_client, session=session)
            break
        except _SwitchAccountRequested:
            _section("Authenticate", step=1, total=total_steps)
            token = _switch_account(args, config)
    workspace_id = str(workspace["id"])
    path = write_profile(profile=args.profile, api_url=authed_client.api_url, workspace=workspace)
    _success(f"Using workspace {workspace_id}")
    _hint(f"Saved Rebase profile '{args.profile}' to {path}")
    _mark_local_workspace(workspace)
    _hint("Run `rebase connect github` when you are ready to add source backing.")
    _success("Setup complete")
    return 0


def run_workspace_create(args: Any) -> int:
    _restore_terminal_for_prompts()
    client = Client(api_url=args.api_url, profile=args.profile)
    config = client.setup_config()
    token = _access_token(args, config)
    authed_client = Client(api_url=client.api_url, access_token=token, profile=args.profile)
    session = load_session()
    if session is not None:
        _success(f"Authenticated as {_session_description(session)}")

    workspace = _create_workspace(args, authed_client, session=session)
    workspace_id = str(workspace["id"])
    path = write_profile(profile=args.profile, api_url=authed_client.api_url, workspace=workspace)
    _success(f"Created workspace {workspace_id}")
    _hint(f"Saved Rebase profile '{args.profile}' to {path}")
    _mark_local_workspace(workspace)
    _hint("Run `rebase connect github` when you are ready to add source backing.")
    _success("Workspace create complete")
    return 0


def run_connect_github(args: Any) -> int:
    _restore_terminal_for_prompts()
    profile = args.profile or selected_profile_name()
    client = Client(api_url=args.api_url, profile=profile)
    config = client.setup_config()
    if not config.get("github_app_configured"):
        raise RebaseWorkflowError("GitHub connection is not configured on this workflow API")
    workspace_id = getattr(client, "workspace_id", None)
    if not isinstance(workspace_id, str) or not workspace_id:
        raise RebaseWorkflowError("no workspace profile configured. Run `rebase setup` first.")
    workspace_repo = _workspace_repo_full_name(client)
    expected_repo = args.repo or workspace_repo

    total_steps = 3
    _section("Workspace GitHub Repo", step=1, total=total_steps)
    connection = _workspace_github_connection(client, repo_full_name=expected_repo)
    if connection is None:
        if expected_repo:
            _hint(f"No workspace-level GitHub repository connection found for {expected_repo}.")
        else:
            _hint("No workspace-level GitHub repository connection found.")
        created_connection = _connect_github(
            _workspace_github_connect_args(args, repo=expected_repo),
            client,
            workspace_id=workspace_id,
        )
        connection = _workspace_github_connection(client, repo_full_name=expected_repo) or created_connection
        if connection is None:
            raise RebaseWorkflowError("workspace GitHub repo connection was not saved")
    else:
        repo_full_name = _repo_full_name_from_connection(connection)
        if args.repo and args.repo.lower() != repo_full_name.lower():
            raise RebaseWorkflowError(
                f"active workspace is already connected to {repo_full_name}, but --repo requested {args.repo}"
            )
        _success(f"Workspace is connected to {repo_full_name}")
    if expected_repo and _repo_full_name_from_connection(connection).lower() != expected_repo.lower():
        raise RebaseWorkflowError(
            "workspace GitHub repo connection mismatch: "
            f"expected {expected_repo}, got {_repo_full_name_from_connection(connection)}"
        )

    _section("Local Git Repository", step=2, total=total_steps)
    _ensure_local_workspace_repo(connection, client=client)

    _section("GitHub App", step=3, total=total_steps)
    _verify_github_app_access(args, client, connection, workspace_id=workspace_id)

    _success("GitHub workspace connection complete")
    return 0
