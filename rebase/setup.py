from __future__ import annotations

import os
import re
import select
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from rebase.auth import (
    AuthError,
    build_supabase_authorize_url,
    exchange_pkce_code,
    generate_pkce_verifier,
    load_access_token,
    load_session,
    pkce_challenge,
    save_session,
)
from rebase.brand import BRAND_BRIGHT_GREEN, BRAND_MEDIUM_GRAY
from rebase.client import Client, RebaseWorkflowError, _parse_github_remote
from rebase.config import write_profile

JOIN_WORKSPACE = "Join an existing workspace"
CREATE_WORKSPACE = "Create a new workspace"
HANDLE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{1,37}[a-z0-9])?$")


class _CallbackHandler(BaseHTTPRequestHandler):
    server: _CallbackServer

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        params = parse_qs(parsed.query)
        self.server.callback_params = {key: values[-1] for key, values in params.items() if values}
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>Authentication complete</title>
    <style>
      html, body {
        height: 100%;
        margin: 0;
      }
      body {
        align-items: center;
        background: #ffffff;
        color: #111111;
        display: flex;
        font: 18px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        justify-content: center;
        text-align: center;
      }
    </style>
  </head>
  <body>
    <main>Authentication complete. You can close this tab.</main>
    <script>
      window.setTimeout(function () {
        window.close();
      }, 100);
    </script>
  </body>
</html>"""
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
BOLD = "\033[1m"
DIM = "\033[2m"
SELECTED_MARKER = f"{_ansi_color(BRAND_BRIGHT_GREEN)}●{RESET}"
UNSELECTED_MARKER = f"{_ansi_color(BRAND_MEDIUM_GRAY)}○{RESET}"


def _paint(text: str, *styles: str) -> str:
    return "".join(styles) + text + RESET


def _intro() -> None:
    print(_paint("Rebase setup", BOLD, GREEN))
    print(_paint("Connect your identity, workspace, and source repository.", DIM, MUTED))
    print()


def _section(title: str) -> None:
    print()
    print(f"{SELECTED_MARKER} {_paint(title, BOLD, GREEN)}")


def _success(message: str) -> None:
    print(f"{_paint('✓', GREEN)} {message}")


def _hint(message: str) -> None:
    print(_paint(message, DIM, MUTED))


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
    if value:
        return value
    suffix = f" [{default}]" if default else ""
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


def _git(args: list[str]) -> str | None:
    try:
        completed = subprocess.run(["git", *args], check=True, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    value = completed.stdout.strip()
    return value or None


def _local_github_remote() -> str | None:
    owner, name = _parse_github_remote(_git(["config", "--get", "remote.origin.url"]))
    return f"{owner}/{name}" if owner and name else None


def _validate_github_repo_full_name(value: str) -> str:
    repo_full_name = value.strip()
    owner, repo = _parse_github_remote(f"https://github.com/{repo_full_name}")
    if owner is None or repo is None or repo_full_name.count("/") != 1:
        raise RebaseWorkflowError("GitHub repository must use the owner/name format")
    return f"{owner}/{repo}"


def _prompt_existing_repo(args: Any) -> str:
    repo_full_name = _prompt(args.repo, "GitHub repository full name", default=_local_github_remote())
    return _validate_github_repo_full_name(repo_full_name)


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
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    if args.no_browser:
        _hint("Open this URL to authenticate:")
        print(auth_url)
    else:
        webbrowser.open(auth_url)
        _hint("Opened browser for Supabase login")
    deadline = time.monotonic() + args.auth_timeout
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


def _access_token(args: Any, config: dict[str, Any]) -> str:
    if not args.force_auth:
        try:
            token = load_access_token()
        except AuthError as exc:
            raise RebaseWorkflowError(str(exc)) from exc
        if token:
            return token
    return _oauth_session(args, config)


def _workspace_by_id(workspaces: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(workspace["id"]): workspace for workspace in workspaces}


def _select_joined_workspace(workspace_id: str, workspaces_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    workspace = workspaces_by_id.get(workspace_id)
    if workspace is None:
        raise RebaseWorkflowError(f"You do not have access to workspace {workspace_id!r}. Ask an owner to invite you.")
    return workspace


def _select_workspace(args: Any, client: Client, *, session: Any | None) -> dict[str, Any]:
    workspaces = client.list_my_workspaces()
    workspaces_by_id = _workspace_by_id(workspaces)
    if args.workspace:
        workspace_id = _normalize_handle(args.workspace, label="Workspace handle")
        if workspace_id not in workspaces_by_id:
            _ensure_profile_handle(args, client, session=session)
            return client.create_workspace(workspace_id, name=args.workspace_name or workspace_id)
        return workspaces_by_id[workspace_id]
    action = _choose(
        "workspace setup",
        [JOIN_WORKSPACE, CREATE_WORKSPACE],
        default=JOIN_WORKSPACE if workspaces else CREATE_WORKSPACE,
        title="Do you want to join an existing workspace or create a new one?",
    )
    if action == JOIN_WORKSPACE:
        workspace_id = _normalize_handle(_prompt(None, "Workspace handle to join"), label="Workspace handle")
        return _select_joined_workspace(workspace_id, workspaces_by_id)
    profile = _ensure_profile_handle(args, client, session=session)
    profile_handle = profile.get("handle")
    default_workspace = profile_handle if isinstance(profile_handle, str) and profile_handle else None
    workspace_id = _normalize_handle(
        _prompt(None, "Workspace handle to create", default=default_workspace),
        label="Workspace handle",
    )
    return client.create_workspace(workspace_id, name=args.workspace_name)


def _wait_for_github_installation(args: Any, client: Client, *, workspace_id: str) -> dict[str, Any]:
    setup_session = client.create_github_setup_session(workspace_id=workspace_id)
    install_url = setup_session["install_url"]
    if args.no_browser:
        _hint("Open this URL to install the Rebase GitHub App:")
        print(install_url)
    else:
        webbrowser.open(install_url)
        _hint("Opened browser for GitHub App installation")
    deadline = time.monotonic() + args.github_timeout
    while time.monotonic() < deadline:
        status = client.get_github_setup_session(setup_session["id"])
        if status["status"] == "installed" and status.get("installation_id") is not None:
            return status
        if status["status"] == "expired":
            raise RebaseWorkflowError("GitHub setup session expired")
        time.sleep(args.poll_interval)
    raise RebaseWorkflowError("timed out waiting for GitHub App installation")


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


def _connect_github(args: Any, client: Client, *, workspace_id: str) -> None:
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
    if args.github_installation_id:
        setup_status = {"installation_id": args.github_installation_id}
    else:
        setup_status = _wait_for_github_installation(args, client, workspace_id=workspace_id)
    installation_id = int(setup_status["installation_id"])
    repo = _select_repo(args, client, installation_id, repo_full_name=repo_full_name)
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


def run_setup(args: Any) -> int:
    _restore_terminal_for_prompts()
    _intro()
    client = Client(api_url=args.api_url, profile=args.profile)
    config = client.setup_config()
    _section("Authenticate")
    token = _access_token(args, config)
    authed_client = Client(api_url=client.api_url, access_token=token, profile=args.profile)
    session = load_session()
    if session is not None:
        _success(f"Authenticated as {session.email or session.user_id or 'Supabase user'}")
    _section("Workspace")
    workspace = _select_workspace(args, authed_client, session=session)
    workspace_id = str(workspace["id"])
    path = write_profile(profile=args.profile, api_url=authed_client.api_url, workspace=workspace)
    _success(f"Using workspace {workspace_id}")
    _hint(f"Saved Rebase profile '{args.profile}' to {path}")
    _section("GitHub")
    if args.github is False:
        _hint("Run `rebase setup` again later to connect GitHub.")
        _success("Setup complete")
        return 0
    if not config.get("github_app_configured"):
        if args.github is True:
            raise RebaseWorkflowError("GitHub connection is not configured on this workflow API")
        _hint("GitHub connection is not available on this workflow API.")
        _success("Setup complete")
        return 0
    should_connect = args.github is True or _confirm("Connect GitHub now?", default=False)
    if should_connect:
        _connect_github(args, authed_client, workspace_id=workspace_id)
    else:
        _hint("Run `rebase setup` again later to connect GitHub.")
    _success("Setup complete")
    return 0
