from __future__ import annotations

import re
import subprocess
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
from rebase.client import Client, RebaseWorkflowError, _parse_github_remote
from rebase.config import write_profile


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


def _prompt(value: str | None, message: str, *, default: str | None = None) -> str:
    if value:
        return value
    suffix = f" [{default}]" if default else ""
    entered = input(f"{message}{suffix}: ").strip()
    if entered:
        return entered
    if default is not None:
        return default
    raise RebaseWorkflowError(f"{message} is required")


def _confirm(message: str, *, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"
    entered = input(f"{message} [{suffix}]: ").strip().lower()
    if not entered:
        return default
    return entered in {"y", "yes"}


def _choose(label: str, values: list[str], *, default: str | None = None) -> str:
    if not values:
        raise RebaseWorkflowError(f"no {label} options available")
    default = default if default in values else values[0]
    print(f"Choose {label}:")
    for index, value in enumerate(values, start=1):
        marker = " (default)" if value == default else ""
        print(f"  {index}. {value}{marker}")
    entered = input(f"{label} [1-{len(values)}]: ").strip()
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
        print(auth_url)
    else:
        webbrowser.open(auth_url)
        print("Opened browser for Supabase login")
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


def _select_workspace(args: Any, client: Client) -> dict[str, Any]:
    workspaces = client.list_my_workspaces()
    workspaces_by_id = {workspace["id"]: workspace for workspace in workspaces}
    workspace_ids = list(workspaces_by_id)
    if args.workspace:
        if args.workspace not in workspaces_by_id:
            return client.create_workspace(args.workspace, name=args.workspace_name)
        return workspaces_by_id[args.workspace]
    if workspace_ids:
        default_workspace = next(
            (workspace["id"] for workspace in workspaces if workspace.get("default")),
            workspace_ids[0],
        )
        selected = _choose("workspace", workspace_ids, default=default_workspace)
        return workspaces_by_id[selected]
    workspace_id = _prompt(None, "Workspace id", default="default")
    return client.create_workspace(workspace_id, name=args.workspace_name)


def _wait_for_github_installation(args: Any, client: Client, *, workspace_id: str) -> dict[str, Any]:
    setup_session = client.create_github_setup_session(workspace_id=workspace_id)
    install_url = setup_session["install_url"]
    if args.no_browser:
        print(install_url)
    else:
        webbrowser.open(install_url)
        print("Opened browser for GitHub App installation")
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
    return _confirm("Create a new GitHub repository?", default=True)


def _create_repo_in_browser(args: Any, *, default_name: str) -> str:
    new_repo_url = f"https://github.com/new?{urlencode({'name': default_name})}"
    if args.no_browser:
        print(new_repo_url)
    else:
        webbrowser.open(new_repo_url)
        print("Opened browser for GitHub repository creation")
    print("Create the repository in GitHub, then return here.")
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
        print("Select that repository when GitHub asks which repositories the Rebase App can access.")
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
    print(f"Connected {connection['repo_owner']}/{connection['repo_name']} at {scope} level")


def run_setup(args: Any) -> int:
    client = Client(api_url=args.api_url, profile=args.profile)
    config = client.setup_config()
    token = _access_token(args, config)
    authed_client = Client(api_url=client.api_url, access_token=token, profile=args.profile)
    session = load_session()
    if session is not None:
        print(f"Authenticated as {session.email or session.user_id or 'Supabase user'}")
    workspace = _select_workspace(args, authed_client)
    workspace_id = str(workspace["id"])
    path = write_profile(profile=args.profile, api_url=authed_client.api_url, workspace=workspace)
    print(f"Using workspace {workspace_id}")
    print(f"Saved Rebase profile '{args.profile}' to {path}")
    if args.github is False:
        print("Run `rebase setup` again later to connect GitHub.")
        return 0
    if not config.get("github_app_configured"):
        if args.github is True:
            raise RebaseWorkflowError("GitHub connection is not configured on this workflow API")
        print("GitHub connection is not available on this workflow API.")
        return 0
    should_connect = args.github is True or _confirm("Connect GitHub now?", default=False)
    if should_connect:
        _connect_github(args, authed_client, workspace_id=workspace_id)
    else:
        print("Run `rebase setup` again later to connect GitHub.")
    return 0
