"""The commands. Each takes the Context and returns nothing (errors are raised as CliError)."""

from __future__ import annotations

import getpass
import os
import platform
import sys
import tempfile
import time
import webbrowser
from pathlib import Path
from typing import Any

from . import __version__, config
from .client import Client, check_url, quote
from .context import Context
from .errors import CONFLICT, FORBIDDEN, NOT_FOUND, USAGE, ApiError, CliError
from .output import emit_json, fields, get, info, size, table, text, when

# -- account and profiles ---------------------------------------------------------------------------------------------


def login(ctx: Context) -> None:
    """`sudoo login`: store a panel's address, API token and signing secret in a profile.

    The address comes from --url, then SUDOO_URL, then a question. The token and the secret come from SUDOO_TOKEN and
    SUDOO_SECRET (or SUDOO_SIGNING_SECRET), else a hidden question. Unless --no-verify is given, the credentials are
    tried with GET /versions first.

    Args:
        ctx: The run context.

    Raises:
        CliError: When a value is missing and there is no terminal to ask in (USAGE), or the panel refuses the
            credentials.

    Side effects:
        Writes the profile and the credentials (keyring or private file); prints where they were kept.
    """
    a = ctx.args
    name = config.profile_name(a.profile)
    url = a.url or os.environ.get("SUDOO_URL") or _ask("Panel address (https://...): ")
    url = check_url(url)
    token = os.environ.get("SUDOO_TOKEN") or _ask("API token (sudoo_...): ", secret=True)
    secret = (
        os.environ.get("SUDOO_SECRET")
        or os.environ.get("SUDOO_SIGNING_SECRET")
        or _ask("Signing secret: ", secret=True)
    )
    profile = config.Profile(name, url, token.strip(), secret.strip(), a.ca_cert, a.acting_for)
    if not a.no_verify:
        client = Client(profile, timeout=a.timeout, debug=a.debug)
        try:
            client.get("/versions")
        except ApiError as error:
            raise CliError(f"The panel refused these credentials: {error.message}", error.code) from None
    where = config.save_profile(name, url, profile.token, profile.secret, a.ca_cert, a.acting_for, a.default)
    stored = "the OS keyring" if where == "keyring" else f"{where} (mode 0600)"
    if ctx.json:
        emit_json({"profile": name, "url": url, "credentials": "keyring" if where == "keyring" else "file"})
    else:
        print(f"Logged in to {url} as profile {name!r}. Token and signing secret are in {stored}.")


def _ask(prompt: str, secret: bool = False) -> str:
    """Ask for one value in the terminal.

    Args:
        prompt: The question.
        secret: When True the answer is not shown while typing.

    Returns:
        The answer, without surrounding spaces.

    Raises:
        CliError: With exit code USAGE when there is no terminal or the answer is empty.
    """
    if not sys.stdin.isatty():
        raise CliError(
            "Not a terminal: give the address with --url and the credentials in SUDOO_TOKEN and SUDOO_SECRET.", USAGE
        )
    value = (getpass.getpass(prompt) if secret else input(prompt)).strip()
    if not value:
        raise CliError("Nothing entered.", USAGE)
    return value


def logout(ctx: Context) -> None:
    """`sudoo logout`: forget a profile and its credentials.

    Args:
        ctx: The run context.
    """
    name = config.profile_name(ctx.args.profile)
    existed = config.forget(name)
    if ctx.json:
        emit_json({"profile": name, "removed": existed})
    else:
        print(f"Profile {name!r} removed." if existed else f"There was no profile {name!r}.")


def profiles(ctx: Context) -> None:
    """`sudoo profiles`: list the stored profiles, marking the default one.

    Args:
        ctx: The run context.
    """
    data = config.load_config()
    default = data.get("default_profile")
    rows = [
        {"name": n, "url": p.get("url"), "credentials": p.get("credentials"), "default": n == default}
        for n, p in sorted(data.get("profiles", {}).items())
    ]
    if ctx.json:
        emit_json(rows)
    else:
        table(
            rows,
            [
                ("profile", get("name")),
                ("url", get("url")),
                ("credentials", get("credentials")),
                ("default", get("default")),
            ],
            "No profiles. Run `sudoo login`.",
        )


def version(ctx: Context) -> None:
    """`sudoo version`: print the version of this command and of Python.

    Args:
        ctx: The run context.
    """
    if ctx.json:
        emit_json({"version": __version__, "python": platform.python_version()})
    else:
        print(f"sudoo-cli {__version__} (Python {platform.python_version()})")


def status(ctx: Context) -> None:
    """`sudoo status`: the panel's health, version, licence, usage and limits.

    The quota, capacity and licence parts are skipped (shown as "-") when the token may not read them or the panel
    has no measurement yet.

    Args:
        ctx: The run context.
    """
    c = ctx.client
    healthy = c.healthz()
    server = c.get("/server").data
    result: dict[str, Any] = {"url": c.base, "profile": c.profile.name, "healthy": healthy, "server": server}
    for key, path in (("quota", "/instance-quota"), ("capacity", "/server/capacity"), ("licence", "/licence")):
        try:
            result[key] = c.get(path).data
        except ApiError:  # a token without the permission, or no measurement yet (503)
            result[key] = None
    if ctx.json:
        emit_json(result)
        return
    metrics = server.get("metrics") or {}
    capacity = result["capacity"] or {}
    quota = result["quota"] or {}
    licence = result["licence"] or {}
    accepted = licence.get("accepted") or {}
    fields(
        [
            ("Panel", f"{c.base} (profile {c.profile.name})"),
            ("Health", "ok" if healthy else "not answering /healthz"),
            ("Version", server.get("version")),
            ("Plugins", server.get("plugins")),
            (
                "Licence",
                f"{accepted.get('version') or licence.get('bundled_version') or '-'}, use {accepted.get('use') or '-'}",
            ),
            (
                "Instances",
                f"{metrics.get('instances_running', '-')} running of {metrics.get('instances_total', quota.get('instances', '-'))}",
            ),
            ("Limit", _limit(quota)),
            ("Capacity", capacity.get("message") or "-"),
            ("CPU", f"{metrics.get('cpu_percent', '-')} %"),
            ("Memory", f"{metrics.get('memory_percent', '-')} %"),
            ("Disk", f"{metrics.get('disk_percent', '-')} %"),
        ]
    )


def _limit(quota: dict[str, Any]) -> str:
    """Describe the instance limit of the panel as text.

    Args:
        quota: The answer of /instance-quota; may be empty.

    Returns:
        For example "3 of 10 instances", with the per-token limit and the next possible creation time when there are
        any; "-" when there is no quota.
    """
    if not quota:
        return "-"
    most = quota.get("max_instances") or "no limit"
    text_ = f"{quota.get('instances', '-')} of {most} instances"
    if quota.get("max_instances_per_user"):
        text_ += f", {quota.get('own_instances')} of {quota['max_instances_per_user']} for this token"
    if quota.get("next_creation_at"):
        text_ += f", next creation at {when(quota['next_creation_at'])}"
    return text_


# -- projects and branches ------------------------------------------------------------------------------------------

# Columns of the projects table.
PROJECT_COLUMNS = [
    ("id", get("id")),
    ("repository", get("repository.full_name")),
    ("odoo", lambda p: f"{get('odoo.version')(p) or '-'} {get('odoo.edition')(p) or ''}".strip()),
    ("branches", get("branches.total")),
    ("instances", get("instances.total")),
    ("attention", get("attention")),
]


def projects_list(ctx: Context) -> None:
    """`sudoo projects list`: every project.

    Args:
        ctx: The run context.
    """
    response = ctx.client.get("/projects")
    if ctx.json:
        emit_json(response.json())
    else:
        table(response.data or [], PROJECT_COLUMNS, "No projects yet.")


def projects_show(ctx: Context) -> None:
    """`sudoo projects show`: a project and its branches.

    Args:
        ctx: The run context; `ctx.args.project` is the project's id and `ctx.args.refresh` reads the branches from
            GitHub first.
    """
    response = ctx.client.get(f"/projects/{quote(ctx.args.project)}", refresh=ctx.args.refresh or None)
    if ctx.json:
        emit_json(response.json())
        return
    p = response.data
    fields(
        [
            ("Project", p.get("id")),
            ("Name", p.get("name")),
            ("Repository", get("repository.url")(p)),
            ("Odoo", f"{get('odoo.version')(p)} {get('odoo.edition')(p)}"),
            ("Origin", p.get("origin")),
            ("Default branch", response.meta.get("default_branch")),
            ("Created", f"{when(p.get('created_at'))} by {p.get('created_by') or '-'}"),
            ("Instances", get("instances.names")(p)),
        ]
    )
    print()
    _branches_table(p.get("branches_list") or [])


def projects_create(ctx: Context) -> None:
    """`sudoo projects create`: create a new, empty GitHub repository and a project for it.

    Args:
        ctx: The run context.

    Side effects:
        The panel creates the repository on GitHub.
    """
    a = ctx.args
    create: dict[str, Any] = {"owner": a.owner, "name": a.repo_name, "private": not a.public}
    if a.description:
        create["description"] = a.description
    body: dict[str, Any] = {"create": create, "odoo_version": a.odoo_version, "edition": a.edition}
    if a.name:
        body["name"] = a.name
    _project_made(ctx, ctx.client.post("/projects", body))


def projects_link(ctx: Context) -> None:
    """`sudoo projects link`: make a project of an existing GitHub repository.

    Args:
        ctx: The run context.
    """
    a = ctx.args
    body: dict[str, Any] = {"repository": a.repository, "odoo_version": a.odoo_version, "edition": a.edition}
    if a.name:
        body["name"] = a.name
    _project_made(ctx, ctx.client.post("/projects", body))


def _project_made(ctx: Context, response: Any) -> None:
    """Print the project that was made.

    Args:
        ctx: The run context.
        response: The panel's answer to the create call.
    """
    if ctx.json:
        emit_json(response.json())
    else:
        p = response.data
        print(f"Project {p.get('id')} ({get('repository.full_name')(p)}), origin {p.get('origin')}.")


def _branches_table(branches: list[dict[str, Any]]) -> None:
    """Print the branches of a project as a table.

    Args:
        branches: The branch objects of the project.
    """
    table(
        branches,
        [
            ("branch", lambda b: str(b.get("name")) + (" (default)" if b.get("default") else "")),
            ("stage", get("stage")),
            ("commit", get("commit.sha")),
            ("subject", lambda b: (get("commit.subject")(b) or "-")[:50]),
            ("instances", get("instances")),
            ("on github", get("on_github")),
        ],
        "No branches.",
    )


def branches_list(ctx: Context) -> None:
    """`sudoo branches list`: a project's branches.

    Args:
        ctx: The run context.
    """
    response = ctx.client.get(f"/projects/{quote(ctx.args.project)}", refresh=ctx.args.refresh or None)
    branches = response.data.get("branches_list") or []
    if ctx.json:
        emit_json({"data": branches, "meta": response.meta})
    else:
        _branches_table(branches)


def branches_set_stage(ctx: Context) -> None:
    """`sudoo branches set-stage`: move a branch, and its instances, to a stage.

    Args:
        ctx: The run context. Moving into or out of production needs --confirm-production.
    """
    a = ctx.args
    body: dict[str, Any] = {"stage": a.stage}
    if a.confirm_production:
        body["confirm_production"] = True
    path = f"/projects/{quote(a.project)}/branches/{quote(a.branch, keep_slash=True)}/stage"
    response = ctx.client.request("PUT", path, body)
    if ctx.json:
        emit_json(response.json())
    else:
        print(f"Branch {a.branch} of {a.project} is now {a.stage}; its instances moved with it.")


def branches_create_instance(ctx: Context) -> None:
    """`sudoo branches create-instance`: make an instance of a branch, in the branch's stage, and follow the job.

    Args:
        ctx: The run context.
    """
    a = ctx.args
    body: dict[str, Any] = {}
    if a.name:
        body["name"] = a.name
    if a.demo is not None:
        body["demo"] = a.demo
    if a.no_install_modules:
        body["install_modules"] = False
    path = f"/projects/{quote(a.project)}/branches/{quote(a.branch, keep_slash=True)}/instances"
    ctx.job(ctx.client.post(path, body).data)


# -- instances --------------------------------------------------------------------------------------------------------

# Columns of the instances table.
INSTANCE_COLUMNS = [
    ("name", get("name")),
    ("status", get("status")),
    ("stage", get("stage")),
    ("project", get("project.id")),
    ("branch", get("git.branch")),
    ("odoo", lambda i: f"{get('odoo.version')(i) or '-'} {(get('odoo.edition')(i) or '')[:3]}".strip()),
    ("url", get("url")),
]


def instances_list(ctx: Context) -> None:
    """`sudoo instances list`: every instance, optionally filtered by project, status and stage.

    Args:
        ctx: The run context. The filters are applied here, after all instances were read.
    """
    items = ctx.client.paged("/instances")
    a = ctx.args
    if a.project:
        items = [i for i in items if get("project.id")(i) == a.project]
    if a.status:
        items = [i for i in items if i.get("status") == a.status]
    if a.stage:
        items = [i for i in items if i.get("stage") == a.stage]
    if ctx.json:
        emit_json(items)
    else:
        table(items, INSTANCE_COLUMNS, "No instances.")


def _instance(ctx: Context, name: str) -> dict[str, Any]:
    """Read one instance.

    Args:
        ctx: The run context.
        name: The instance's name.

    Returns:
        The instance object.
    """
    data: dict[str, Any] = ctx.client.get(f"/instances/{quote(name)}").data
    return data


def instances_show(ctx: Context) -> None:
    """`sudoo instances show`: one instance with its usage and any running job.

    Args:
        ctx: The run context.
    """
    response = ctx.client.get(f"/instances/{quote(ctx.args.instance)}")
    if ctx.json:
        emit_json(response.json())
        return
    i = response.data
    usage = i.get("usage") or {}
    pairs: list[tuple[str, Any]] = [
        ("Instance", i.get("name")),
        ("Status", i.get("status")),
        ("Stage", i.get("stage")),
        ("URL", i.get("url")),
        ("Project", get("project.id")(i)),
        ("Odoo", f"{get('odoo.version')(i)} {get('odoo.edition')(i)}, database {get('odoo.database')(i)}"),
        ("Repository", get("git.repository")(i)),
        ("Branch", get("git.branch")(i)),
        ("Commit", f"{get('git.commit.sha')(i) or '-'} {get('git.commit.subject')(i) or ''}".strip()),
        ("Settings", i.get("settings")),
        ("CPU", f"{usage.get('cpu_percent', '-')} %"),
        ("Memory", f"{size(usage.get('memory_bytes'))} of {size(usage.get('memory_limit_bytes'))}"),
        ("Disk", size(usage.get("disk_bytes"))),
        ("Created", when(i.get("created_at"))),
        ("Pauses at", when(i.get("pauses_at"))),
        ("Expires at", when(i.get("expires_at"))),
    ]
    if i.get("current_job"):
        job = i["current_job"]
        pairs.append(("Running job", f"{job.get('action')} ({job.get('status')}), sudoo jobs follow {job.get('id')}"))
    if i.get("update_failure"):
        pairs.append(("Last update failed", get("update_failure.message")(i)))
    fields(pairs)


def instances_create(ctx: Context) -> None:
    """`sudoo instances create`: make a new instance from a repository's branch, or plain Odoo, and follow the job.

    Args:
        ctx: The run context.
    """
    a = ctx.args
    body: dict[str, Any] = {"version": a.odoo_version, "edition": a.edition, "stage": a.stage}
    for key, value in (
        ("repo", a.repo),
        ("branch", a.branch),
        ("name", a.name),
        ("delete_after_days", a.delete_after_days),
        ("from_backup", a.from_backup),
        ("demo", a.demo),
    ):
        if value is not None:
            body[key] = value
    if a.no_install_modules:
        body["install_modules"] = False
    response = ctx.client.post("/instances", body)
    if not ctx.json and response.headers.get("Content-Location"):
        info(f"New instance: {response.data.get('instance') or response.headers['Content-Location']}")
    ctx.job(response.data)


def instance_action(action: str) -> Any:
    """start, pause, restart, stop (switch-off) and the like: POST /instances/<name>/actions/<action>."""

    def run(ctx: Context) -> None:
        """Run the action on the instance named in the arguments and follow the job.

        Args:
            ctx: The run context. `--confirm-production` is sent when the command has it.
        """
        body: dict[str, Any] = {}
        if getattr(ctx.args, "confirm_production", False):
            body["confirm_production"] = True
        ctx.job(ctx.client.post(f"/instances/{quote(ctx.args.instance)}/actions/{action}", body).data)

    return run


def instances_delete(ctx: Context) -> None:
    """`sudoo instances delete`: delete an instance, after a backup, and follow the job.

    The person must type the instance's name to confirm, unless --yes is given.

    Args:
        ctx: The run context.

    Raises:
        CliError: When the confirmation is refused (USAGE) or the panel refuses.
    """
    a = ctx.args
    name = a.instance
    ctx.confirm(f"Delete instance {name} (a backup is taken first)?", expected=name)
    body: dict[str, Any] = {"confirm": name}
    if a.confirm_production:
        body["confirm_stage"] = "production"
    if a.without_backup:
        body["without_backup"] = True
    ctx.job(ctx.client.request("DELETE", f"/instances/{quote(name)}", body).data)


def instances_set(ctx: Context) -> None:
    """`sudoo instances set`: change an instance's settings or stage.

    Args:
        ctx: The run context.

    Raises:
        CliError: With exit code USAGE when no option was given.
    """
    a = ctx.args
    body: dict[str, Any] = {}
    for key in ("auto_update", "auto_upgrade", "auto_pause", "delete_after_days", "stage"):
        value = getattr(a, key)
        if value is not None:
            body[key] = value
    if a.confirm_production:
        body["confirm_production"] = True
    if not body:
        raise CliError("Nothing to change: give at least one option (see sudoo instances set --help).", USAGE)
    response = ctx.client.request("PATCH", f"/instances/{quote(a.instance)}", body)
    if ctx.json:
        emit_json(response.json())
    else:
        print(f"{a.instance}: {', '.join(f'{k} = {text(v)}' for k, v in body.items() if k != 'confirm_production')}")


def instances_logs(ctx: Context) -> None:
    """`sudoo instances logs`: print the end of Odoo's log, and with --follow keep printing new lines until stopped.

    Args:
        ctx: The run context.
    """
    a = ctx.args
    path = f"/instances/{quote(a.instance)}/logs"
    lines = ctx.client.get(path, lines=a.lines).data.get("text", "").splitlines()
    if ctx.json and not a.follow:
        emit_json({"lines": lines})
        return
    for line in lines:
        print(line, flush=True)
    while a.follow:
        ctx.client.sleep(a.interval)
        latest = ctx.client.get(path, lines=max(a.lines, 200)).data.get("text", "").splitlines()
        for line in new_lines(lines, latest):
            print(line, flush=True)
        lines = latest


def new_lines(before: list[str], after: list[str]) -> list[str]:
    """The lines of `after` (a later tail of the same log) that `before` did not show yet."""
    for overlap in range(min(len(before), len(after)), 0, -1):
        if before[-overlap:] == after[:overlap]:
            return after[overlap:]
    return after


def instances_open(ctx: Context) -> None:
    """`sudoo instances open`: print an instance's address and open it in a browser when run in a terminal.

    Args:
        ctx: The run context.

    Raises:
        CliError: With exit code CONFLICT when the instance has no address yet.
    """
    instance = _instance(ctx, ctx.args.instance)
    url = instance.get("url")
    if not url:
        raise CliError(f"{ctx.args.instance} has no address yet (status {instance.get('status')}).", CONFLICT)
    if ctx.json:
        emit_json({"name": instance.get("name"), "url": url})
        return
    print(url)
    if not ctx.args.print_only and sys.stdout.isatty():
        webbrowser.open(url)


def deploy(ctx: Context) -> None:
    """Get the latest code: the `update` action on one instance, or on every instance of a project's branch."""
    a = ctx.args
    if a.instance and (a.project or a.branch):
        raise CliError("Give an instance, or --project and --branch, not both.", USAGE)
    if a.instance:
        names = [a.instance]
    elif a.project and a.branch:
        project = ctx.client.get(f"/projects/{quote(a.project)}", refresh=1).data
        branch = next((b for b in project.get("branches_list") or [] if b.get("name") == a.branch), None)
        if branch is None:
            raise CliError(f"The project {a.project} has no branch {a.branch}.", NOT_FOUND)
        names = _instance_names(branch.get("instances"))
        if not names:
            raise CliError(
                f"Branch {a.branch} has no instance. Create one: sudoo branches create-instance {a.project} {a.branch}",
                NOT_FOUND,
            )
    else:
        raise CliError("Give an instance, or --project and --branch.", USAGE)
    action = "update-submodules" if a.submodules else "update"
    for name in names:
        ctx.job(ctx.client.post(f"/instances/{quote(name)}/actions/{action}", {}).data)
    if not ctx.json and not a.no_wait:
        for name in names:
            print(f"{name}: up to date")


def _instance_names(value: Any) -> list[str]:
    """Read instance names from the shapes the panel uses.

    Args:
        value: A list of names or objects with a "name", an object with "names", a single name, or something else.

    Returns:
        The names; an empty list when there are none.
    """
    if isinstance(value, list):
        return [v if isinstance(v, str) else str(v.get("name")) for v in value if v]
    if isinstance(value, dict):
        return [str(n) for n in value.get("names") or []]
    if isinstance(value, str):
        return [value]
    return []


# -- backups ----------------------------------------------------------------------------------------------------------

# Columns of the backups table. Both the older and the newer field names for size and time are accepted.
BACKUP_COLUMNS = [
    ("file", get("file")),
    ("instance", get("instance")),
    ("kind", get("kind")),
    ("odoo", get("series")),
    ("size", lambda b: size(b.get("size", b.get("size_bytes")))),
    ("created", lambda b: when(b.get("created", b.get("created_at")))),
]
# Columns of the off-site copies table.
OFFSITE_COLUMNS = [
    ("id", get("id")),
    ("instance", get("instance")),
    ("kind", get("kind")),
    ("status", get("status")),
    ("size", lambda b: size(b.get("size"))),
    ("created", lambda b: when(b.get("created"))),
    ("verified", lambda b: when(b.get("verified_at"))),
]


def backups_list(ctx: Context) -> None:
    """`sudoo backups list`: backups (or off-site copies with --offsite), optionally of one instance.

    When the token may not list all backups (403), the backups of the named instance are read from the instance.

    Args:
        ctx: The run context.
    """
    a = ctx.args
    if a.offsite:
        items = ctx.client.get("/offsite/copies").data or []
        columns = OFFSITE_COLUMNS
    else:
        try:
            items = ctx.client.get("/backups").data or []
        except ApiError as error:
            if error.status != 403 or not a.instance:
                raise
            # Without backups.manage, an instance's own backups are still in GET /instances/<name>.
            items = [dict(b, instance=a.instance) for b in _instance(ctx, a.instance).get("backups") or []]
        columns = BACKUP_COLUMNS
    if a.instance:
        items = [b for b in items if b.get("instance") == a.instance]
    if ctx.json:
        emit_json(items)
    else:
        table(items, columns, "No backups.")


def backups_create(ctx: Context) -> None:
    """`sudoo backups create`: back an instance up now and follow the job.

    Args:
        ctx: The run context.
    """
    ctx.job(ctx.client.post(f"/instances/{quote(ctx.args.instance)}/actions/backup", {}).data)


def backups_restore(ctx: Context) -> None:
    """`sudoo backups restore`: restore a backup, or an off-site copy, into an instance and follow the job.

    Asks for confirmation unless --yes is given. The panel takes a before-restore backup first.

    Args:
        ctx: The run context.

    Raises:
        CliError: When both --file and --offsite are given (USAGE) or the confirmation is refused.
    """
    a = ctx.args
    if a.file and a.offsite:
        raise CliError("Give --file or --offsite, not both.", USAGE)
    what = a.file or (f"off-site copy {a.offsite}" if a.offsite else "the latest backup")
    ctx.confirm(f"Replace the database and files of {a.instance} with {what}? A before-restore backup is taken first.")
    body: dict[str, Any] = {}
    if a.confirm_production:
        body["confirm_production"] = True
    if a.offsite:
        body["instance"] = a.instance
        if a.public_key:
            body["public_key"] = _read_text(a.public_key)
        response = ctx.client.post(f"/offsite/copies/{quote(a.offsite)}/restore", body)
    else:
        if a.file:
            body["file"] = a.file
        response = ctx.client.post(f"/instances/{quote(a.instance)}/actions/restore", body)
    ctx.job(response.data)


def backups_download(ctx: Context) -> None:
    """`sudoo backups download`: save a backup file.

    The data is written to a temporary file first, which replaces the target only when the download is complete.

    Args:
        ctx: The run context.

    Raises:
        CliError: When the name is not a plain file name or the target exists without --force (USAGE).

    Side effects:
        Writes a file; shows the downloaded size on stderr when it is a terminal.
    """
    a = ctx.args
    name = Path(a.file).name
    if name != a.file or name in ("", ".", ".."):
        raise CliError("Give the backup's file name as `sudoo backups list` shows it.", USAGE)
    target = Path(a.output) if a.output else Path(name)
    if target.is_dir():
        target = target / name
    if target.exists() and not a.force:
        raise CliError(f"{target} exists; pass --force to replace it.", USAGE)
    directory = target.parent if str(target.parent) else Path(".")
    fd, temporary = tempfile.mkstemp(prefix=".sudoo-", suffix=".part", dir=directory)
    started = time.monotonic()
    shown = [0.0]

    def progress(done: int) -> None:
        """Show the number of bytes saved, at most every half second and only on a terminal.

        Args:
            done: Bytes saved so far.
        """
        if not ctx.quiet and sys.stderr.isatty() and time.monotonic() - shown[0] > 0.5:
            shown[0] = time.monotonic()
            print(f"\r  {size(done)}", end="", file=sys.stderr, flush=True)

    try:
        with os.fdopen(fd, "wb") as out:
            total = ctx.client.download(f"/backups/{quote(name)}", out, progress)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    if ctx.json:
        emit_json({"file": name, "path": str(target), "size": total})
    else:
        if sys.stderr.isatty() and not ctx.quiet:
            print(file=sys.stderr)
        print(f"Saved {target} ({size(total)} in {time.monotonic() - started:.0f} s).")


def backups_verify(ctx: Context) -> None:
    """`sudoo backups verify`: download and check an off-site copy, and follow the job.

    Args:
        ctx: The run context.
    """
    a = ctx.args
    body: dict[str, Any] = {}
    if a.public_key:
        body["public_key"] = _read_text(a.public_key)
    ctx.job(ctx.client.post(f"/offsite/copies/{quote(a.copy)}/verify", body).data)


def backups_offsite(ctx: Context) -> None:
    """`sudoo backups offsite`: the off-site copy settings and state.

    Args:
        ctx: The run context.
    """
    response = ctx.client.get("/offsite")
    if ctx.json:
        emit_json(response.json())
        return
    o = response.data
    fields(
        [
            ("Off-site copies", "on" if o.get("enabled") else "off"),
            ("Configured", o.get("configured")),
            ("Active", o.get("active")),
            ("Location", o.get("location")),
            ("Stages", o.get("other_stages")),
            ("Kinds", o.get("kinds")),
            ("Key fingerprint", o.get("fingerprint")),
            ("Last scan", o.get("last_scan")),
        ]
    )


def _read_text(path: str) -> str:
    """Read a text file given on the command line.

    Args:
        path: The file's path.

    Returns:
        The content.

    Raises:
        CliError: With exit code USAGE when the file cannot be read.
    """
    try:
        return Path(path).read_text()
    except OSError as error:
        raise CliError(f"Cannot read {path}: {error}", USAGE) from None


# -- domains ----------------------------------------------------------------------------------------------------------


def domains_list(ctx: Context) -> None:
    """`sudoo domains list`: an instance's custom domains and where to point DNS.

    Args:
        ctx: The run context.
    """
    response = ctx.client.get(f"/instances/{quote(ctx.args.instance)}/domains")
    if ctx.json:
        emit_json(response.json())
        return
    d = response.data
    target = d.get("target") or {}
    fields(
        [
            ("Custom domains", d.get("mode")),
            ("Default address", d.get("default_url")),
            ("Point DNS to", f"{target.get('record_type')} {target.get('host')}" if target else "-"),
            ("Limit", d.get("limit")),
        ]
    )
    print()
    table(
        d.get("domains") or [],
        [
            ("domain", lambda x: str(x.get("domain")) + (" (primary)" if x.get("primary") else "")),
            ("status", get("status")),
            ("certificate expires", get("certificate.expires_at")),
            ("reason", get("reason")),
        ],
        "No custom domains.",
    )


def _domain_body(ctx: Context, **extra: Any) -> dict[str, Any]:
    """Make the JSON body of a domain call.

    Args:
        ctx: The run context.
        **extra: Fields to send.

    Returns:
        The fields, plus confirm_production when --confirm-production was given.
    """
    body = dict(extra)
    if getattr(ctx.args, "confirm_production", False):
        body["confirm_production"] = True
    return body


def _domain_path(ctx: Context, suffix: str = "") -> str:
    """Make the API path of the domain in the arguments.

    Args:
        ctx: The run context.
        suffix: Text to add after the domain, for example "/check".

    Returns:
        The path below /api/v1.
    """
    return f"/instances/{quote(ctx.args.instance)}/domains/{quote(ctx.args.domain)}{suffix}"


def _show_domain(ctx: Context, domain: dict[str, Any]) -> None:
    """Print a domain: its state, certificate and the DNS records to create.

    Args:
        ctx: The run context.
        domain: The domain object.
    """
    if ctx.json:
        emit_json(domain)
        return
    fields(
        [
            ("Domain", domain.get("domain")),
            ("Status", domain.get("status")),
            ("Reason", domain.get("reason")),
            ("Primary", domain.get("primary")),
            ("URL", domain.get("url")),
            ("Certificate", domain.get("certificate")),
            ("Checked", when(domain.get("checked_at"))),
            ("Next check", when(domain.get("next_check_at"))),
        ]
    )
    records = domain.get("dns_records") or []
    if records:
        print("\nDNS records to create:")
        table(records, [("type", get("type")), ("name", get("name")), ("value", get("value"))])


def domains_add(ctx: Context) -> None:
    """`sudoo domains add`: add a domain to an instance.

    Args:
        ctx: The run context.
    """
    body = _domain_body(ctx, domain=ctx.args.domain)
    _show_domain(ctx, ctx.client.post(f"/instances/{quote(ctx.args.instance)}/domains", body).data)


def domains_remove(ctx: Context) -> None:
    """`sudoo domains remove`: remove a domain; its certificate is deleted too. Asks for confirmation unless --yes.

    Args:
        ctx: The run context.
    """
    ctx.confirm(f"Remove {ctx.args.domain} from {ctx.args.instance} (its certificate is deleted)?")
    ctx.client.request("DELETE", _domain_path(ctx), _domain_body(ctx) or None)
    if ctx.json:
        emit_json({"removed": ctx.args.domain})
    else:
        print(f"{ctx.args.domain} removed.")


def domains_status(ctx: Context) -> None:
    """`sudoo domains status`: one domain's state and DNS records; with --check the panel checks DNS now.

    Args:
        ctx: The run context.

    Raises:
        CliError: With exit code NOT_FOUND when the instance has no such domain.
    """
    if ctx.args.check:
        domain = ctx.client.post(_domain_path(ctx, "/check")).data
    else:
        data = ctx.client.get(f"/instances/{quote(ctx.args.instance)}/domains").data
        domain = next((d for d in data.get("domains") or [] if d.get("domain") == ctx.args.domain), None)
        if domain is None:
            raise CliError(f"{ctx.args.instance} has no domain {ctx.args.domain}.", NOT_FOUND)
    _show_domain(ctx, domain)


def domains_primary(ctx: Context) -> None:
    """`sudoo domains primary`: make a domain the primary one.

    Args:
        ctx: The run context.
    """
    _show_domain(ctx, ctx.client.request("PATCH", _domain_path(ctx), _domain_body(ctx, primary=True)).data)


# -- shell, mail, resources -------------------------------------------------------------------------------------------


def shell(ctx: Context) -> None:
    """The panel gives shell tickets to people signed in to it, never to API tokens (docs/PANEL_API.md, 12)."""
    try:
        base = ctx.client.base
    except CliError:
        base = "https://<your panel>"
    raise CliError(
        "The panel opens shells only for people signed in to its web app: API tokens cannot get a shell ticket. "
        f"Open the Shell tab instead: {base}/i/{quote(ctx.args.instance)}/shell",
        FORBIDDEN,
    )


def mail_list(ctx: Context) -> None:
    """`sudoo mail list`: the e-mails caught from a staging or development instance, newest first.

    Args:
        ctx: The run context.
    """
    a = ctx.args
    response = ctx.client.get(f"/instances/{quote(a.instance)}/mail", q=a.search, limit=a.limit, offset=a.offset)
    if ctx.json:
        emit_json(response.json())
        return
    table(
        response.data or [],
        [
            ("id", get("id")),
            ("received", lambda m: when(m.get("received_at"))),
            ("from", get("from")),
            ("to", get("to")),
            ("subject", lambda m: (m.get("subject") or "")[:60]),
        ],
        "No caught e-mail.",
    )
    meta = response.meta
    if meta.get("total") is not None:
        info(f"{meta.get('count', len(response.data or []))} of {meta['total']} e-mails.")


def mail_show(ctx: Context) -> None:
    """`sudoo mail show`: one caught e-mail with its text.

    Args:
        ctx: The run context.
    """
    response = ctx.client.get(f"/instances/{quote(ctx.args.instance)}/mail/{quote(ctx.args.id)}")
    if ctx.json:
        emit_json(response.json())
        return
    m = response.data
    fields(
        [
            ("From", m.get("from")),
            ("To", m.get("to")),
            ("Cc", m.get("cc")),
            ("Subject", m.get("subject")),
            ("Date", m.get("date")),
            ("Attachments", [a.get("filename") for a in m.get("attachment_list") or []]),
        ]
    )
    print()
    print(m.get("text") or "(no text part)")


# The resource limits a person can set with `sudoo resources set`.
RESOURCE_KEYS = (
    "odoo_workers",
    "odoo_cron_threads",
    "odoo_limit_memory_soft_mb",
    "odoo_limit_memory_hard_mb",
    "odoo_limit_time_cpu",
    "odoo_limit_time_real",
    "odoo_memory_limit",
    "odoo_cpu_limit",
    "db_memory_limit",
    "db_cpu_limit",
)


def resources_show(ctx: Context) -> None:
    """`sudoo resources show`: the limits that apply to an instance, its own values and the stage defaults.

    Args:
        ctx: The run context.
    """
    response = ctx.client.get(f"/instances/{quote(ctx.args.instance)}/resources")
    if ctx.json:
        emit_json(response.json())
        return
    r = response.data
    limits, own, stage = r.get("limits") or {}, r.get("own") or {}, r.get("stage_defaults") or {}
    rows = [
        {"key": k, "value": limits.get(k), "own": own.get(k), "stage": stage.get(k)}
        for k in sorted(set(limits) | set(own) | set(RESOURCE_KEYS))
    ]
    fields([("Stage", r.get("stage")), ("Priority", get("priority.rank")(r)), ("Restart pending", r.get("pending"))])
    print()
    table(
        rows, [("limit", get("key")), ("applies", get("value")), ("own", get("own")), ("stage default", get("stage"))]
    )
    for warning in r.get("warnings") or []:
        info(f"warning: {warning.get('text')}")


def resources_set(ctx: Context) -> None:
    """`sudoo resources set`: change limits given as KEY=VALUE and follow the job.

    The instance's current own limits are read first, and the given keys are changed in them.

    Args:
        ctx: The run context.

    Raises:
        CliError: With exit code USAGE when an item is not KEY=VALUE or the key is not in `RESOURCE_KEYS`.
    """
    a = ctx.args
    path = f"/instances/{quote(a.instance)}/resources"
    own: dict[str, Any] = dict(ctx.client.get(path).data.get("own") or {})
    for item in a.limits:
        key, sep, value = item.partition("=")
        key = key.strip()
        if not sep or key not in RESOURCE_KEYS:
            raise CliError(f"Give limits as KEY=VALUE, KEY one of: {', '.join(RESOURCE_KEYS)}.", USAGE)
        own[key] = _limit_value(value.strip())
    body: dict[str, Any] = {"limits": own}
    if a.confirm_production:
        body["confirm_production"] = True
    ctx.job(ctx.client.request("PUT", path, body).data)


def _limit_value(value: str) -> Any:
    """Read a limit value typed on the command line.

    Args:
        value: The text after "=".

    Returns:
        None for "", "default", "null" or "none" (the limit follows the stage again); an int or float when it is a
        number; otherwise the text, for example "4g".
    """
    if value.lower() in ("", "default", "null", "none"):
        return None  # follows the stage again
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value  # "4g"


# -- jobs and users ---------------------------------------------------------------------------------------------------

# Columns of the jobs table.
JOB_COLUMNS = [
    ("id", get("id")),
    ("instance", get("instance")),
    ("action", get("action")),
    ("status", get("status")),
    ("progress", lambda j: f"{j['progress_percent']}%" if isinstance(j.get("progress_percent"), int) else "-"),
    ("by", get("requested_by")),
    ("started", lambda j: when(j.get("started_at"))),
]


def jobs_list(ctx: Context) -> None:
    """`sudoo jobs list`: recent jobs, newest first.

    Args:
        ctx: The run context.
    """
    a = ctx.args
    response = ctx.client.get("/jobs", instance=a.instance, limit=a.limit, offset=a.offset)
    if ctx.json:
        emit_json(response.json())
    else:
        table(response.data or [], JOB_COLUMNS, "No jobs.")


def jobs_show(ctx: Context) -> None:
    """`sudoo jobs show`: one job with its steps and notes; with --log also its technical log.

    Args:
        ctx: The run context.
    """
    response = ctx.client.get(f"/jobs/{quote(ctx.args.job)}", include="log" if ctx.args.log else None)
    if ctx.json:
        emit_json(response.json())
        return
    j = response.data
    fields(
        [
            ("Job", j.get("id")),
            ("Instance", j.get("instance")),
            ("Action", j.get("action")),
            ("Status", j.get("status")),
            ("Progress", j.get("progress_percent")),
            ("Requested by", j.get("requested_by")),
            ("Started", when(j.get("started_at"))),
            ("Finished", when(j.get("finished_at"))),
            ("Error", j.get("error")),
        ]
    )
    for step in j.get("steps") or []:
        print(f"  {step.get('status', ''):<8} {step.get('title')}")
    for note in j.get("notes") or []:
        print(f"  note: {note}")
    if j.get("log"):
        print()
        print(j["log"] if isinstance(j["log"], str) else text(j["log"]))


def jobs_follow(ctx: Context) -> None:
    """`sudoo jobs follow`: follow a job until it ends; the exit code is JOB_FAILED when it failed.

    Args:
        ctx: The run context.
    """
    ctx.job(ctx.client.get(f"/jobs/{quote(ctx.args.job)}").data)


def jobs_log(ctx: Context) -> None:
    """`sudoo jobs log`: print a job's technical log from an offset.

    Args:
        ctx: The run context.
    """
    response = ctx.client.get(f"/jobs/{quote(ctx.args.job)}/log", offset=ctx.args.offset)
    if ctx.json:
        emit_json(response.json())
    else:
        sys.stdout.write(response.data.get("text", ""))


def users_list(ctx: Context) -> None:
    """`sudoo users list`: every account of the panel (needs a write token).

    Args:
        ctx: The run context.
    """
    response = ctx.client.get("/users")
    if ctx.json:
        emit_json(response.json())
        return
    table(
        response.data or [],
        [
            ("user", get("name")),
            ("name", get("display_name")),
            ("email", get("email")),
            ("role", get("role_label")),
            ("second factor", get("second_factors")),
            ("last sign-in", lambda u: when(u.get("last_sign_in_at"))),
        ],
        "No accounts.",
    )


def roles_list(ctx: Context) -> None:
    """`sudoo users roles`: the roles of the panel and what they are for.

    Args:
        ctx: The run context.
    """
    response = ctx.client.get("/roles")
    if ctx.json:
        emit_json(response.json())
        return
    table(
        response.data.get("roles") or [],
        [("role", get("name")), ("label", get("label")), ("description", get("description"))],
    )


# -- teams (premium: Pro and Business) --------------------------------------------------------------------------------

# The access levels a team can have in a project or branch, from lowest to highest.
ACCESS_LEVELS = ("none", "view", "developer", "maintainer", "admin")


def _team(ctx: Context, ref: str) -> dict[str, Any]:
    """A team by its id or its name (case aside)."""
    teams: list[dict[str, Any]] = ctx.client.get("/teams").data or []
    for team in teams:
        if team.get("id") == ref:
            return team
    wanted = ref.casefold()
    for team in teams:
        if str(team.get("name", "")).casefold() == wanted:
            return team
    raise CliError(f"There is no team {ref}.", NOT_FOUND)


def _member_names(team: dict[str, Any]) -> list[str]:
    """List the account names of a team's members.

    Args:
        team: The team object.

    Returns:
        The names.
    """
    return [str(m.get("name")) if isinstance(m, dict) else str(m) for m in team.get("members") or []]


def _show_team(ctx: Context, team: dict[str, Any]) -> None:
    """Print a team: its details and its members.

    Args:
        ctx: The run context.
        team: The team object.
    """
    if ctx.json:
        emit_json(team)
        return
    fields(
        [
            ("Team", f"{team.get('name')} ({team.get('id')})"),
            ("Description", team.get("description") or "-"),
            ("Projects", ", ".join(str(p.get("name") or p.get("id")) for p in team.get("projects") or []) or "-"),
            ("Created", f"{when(team.get('created_at'))} by {team.get('created_by') or '-'}"),
            ("Changed", f"{when(team.get('changed_at'))} by {team.get('changed_by') or '-'}"),
        ]
    )
    print()
    table(
        [m for m in team.get("members") or [] if isinstance(m, dict)],
        [("member", get("name")), ("name", get("display_name")), ("role", get("role"))],
        "No members.",
    )


def teams_list(ctx: Context) -> None:
    """`sudoo teams list`: every team with its members and the projects that name it.

    Args:
        ctx: The run context.
    """
    response = ctx.client.get("/teams")
    if ctx.json:
        emit_json(response.json())
        return
    table(
        response.data or [],
        [
            ("id", get("id")),
            ("team", get("name")),
            ("members", lambda t: len(t.get("members") or [])),
            ("projects", lambda t: ", ".join(str(p.get("id")) for p in t.get("projects") or []) or "-"),
            ("description", lambda t: (t.get("description") or "-")[:50]),
        ],
        "No teams.",
    )


def teams_show(ctx: Context) -> None:
    """`sudoo teams show`: a team, found by id or name.

    Args:
        ctx: The run context.
    """
    _show_team(ctx, _team(ctx, ctx.args.team))


def teams_create(ctx: Context) -> None:
    """`sudoo teams create`: make a team, optionally with members.

    Args:
        ctx: The run context.
    """
    a = ctx.args
    body: dict[str, Any] = {"name": a.name}
    if a.description is not None:
        body["description"] = a.description
    if a.member:
        body["members"] = sorted(set(a.member))
    _show_team(ctx, ctx.client.post("/teams", body).data)


def _change_team(ctx: Context, team: dict[str, Any], body: dict[str, Any]) -> None:
    """Change a team with PATCH and print the result.

    Args:
        ctx: The run context.
        team: The team to change.
        body: The fields to change.
    """
    _show_team(ctx, ctx.client.request("PATCH", f"/teams/{quote(str(team['id']))}", body).data)


def teams_rename(ctx: Context) -> None:
    """`sudoo teams rename`: give a team a new name, and optionally a new description.

    Args:
        ctx: The run context.
    """
    a = ctx.args
    body: dict[str, Any] = {"name": a.name}
    if a.description is not None:
        body["description"] = a.description
    _change_team(ctx, _team(ctx, a.team), body)


def teams_delete(ctx: Context) -> None:
    """`sudoo teams delete`: delete a team. Asks for confirmation unless --yes.

    Args:
        ctx: The run context.
    """
    team = _team(ctx, ctx.args.team)
    ctx.confirm(f"Delete the team {team.get('name')} (it leaves every project grant and branch lock)?")
    ctx.client.request("DELETE", f"/teams/{quote(str(team['id']))}")
    if ctx.json:
        emit_json({"deleted": team.get("id")})
    else:
        print(f"Team {team.get('name')} deleted.")


def teams_members_add(ctx: Context) -> None:
    """`sudoo teams members add`: add accounts to a team.

    Args:
        ctx: The run context.
    """
    team = _team(ctx, ctx.args.team)
    members = sorted(set(_member_names(team)) | set(ctx.args.accounts))
    _change_team(ctx, team, {"members": members})


def teams_members_remove(ctx: Context) -> None:
    """`sudoo teams members remove`: take accounts out of a team.

    Args:
        ctx: The run context.

    Raises:
        CliError: With exit code NOT_FOUND when an account is not in the team.
    """
    team = _team(ctx, ctx.args.team)
    current = _member_names(team)
    missing = sorted(set(ctx.args.accounts) - set(current))
    if missing:
        raise CliError(
            f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not in {team.get('name')}.", NOT_FOUND
        )
    _change_team(ctx, team, {"members": sorted(set(current) - set(ctx.args.accounts))})


def _access_body(access: dict[str, Any]) -> dict[str, Any]:
    """The project's access as PUT /projects/<id>/access takes it (without the names the panel adds)."""

    def plain(grants: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep only type, id and level of each grant.

        Args:
            grants: The grants as the panel shows them.

        Returns:
            The reduced grants.
        """
        return [{"type": g.get("type"), "id": g.get("id"), "level": g.get("level")} for g in grants or []]

    return {
        "mode": access.get("mode"),
        "grants": plain(access.get("grants") or []),
        "branches": {
            name: {"restricted": bool(b.get("restricted")), "grants": plain(b.get("grants") or [])}
            for name, b in (access.get("branches") or {}).items()
        },
    }


def _show_access(ctx: Context, project: str, access: dict[str, Any]) -> None:
    """Print a project's access settings as a table of grants.

    Args:
        ctx: The run context.
        project: The project's id.
        access: The access object.
    """
    if ctx.json:
        emit_json(access)
        return
    rows = [{"where": "project", **g} for g in access.get("grants") or []]
    for name, branch in sorted((access.get("branches") or {}).items()):
        restricted = " (restricted)" if branch.get("restricted") else ""
        rows += [{"where": f"branch {name}{restricted}", **g} for g in branch.get("grants") or []]
    print(f"Project {project}: {access.get('mode')}")
    table(
        rows,
        [
            ("where", get("where")),
            ("type", get("type")),
            ("id", get("id")),
            ("name", get("name")),
            ("level", get("level")),
        ],
        "No grants: the panel roles decide.",
    )


def teams_grants_list(ctx: Context) -> None:
    """`sudoo teams grants list`: a project's grants and its branches' grants.

    Args:
        ctx: The run context.
    """
    project = ctx.args.project
    _show_access(ctx, project, ctx.client.get(f"/projects/{quote(project)}/access").data)


def _change_grant(ctx: Context, level: str | None) -> None:
    """Give the team a level in the project or one of its branches (level None: take its grant away)."""
    a = ctx.args
    team = _team(ctx, a.team)
    path = f"/projects/{quote(a.project)}/access"
    body = _access_body(ctx.client.get(path).data)
    if a.branch:
        branch = body["branches"].setdefault(a.branch, {"restricted": False, "grants": []})
        grants = branch["grants"]
    else:
        grants = body["grants"]
    others = [g for g in grants if not (g.get("type") == "team" and g.get("id") == team["id"])]
    if level is None and len(others) == len(grants):
        where = f"branch {a.branch} of {a.project}" if a.branch else a.project
        raise CliError(f"The team {team.get('name')} has no grant in {where}.", NOT_FOUND)
    if level is not None:
        others.append({"type": "team", "id": team["id"], "level": level})
    grants[:] = others
    if a.branch and not grants and not body["branches"][a.branch]["restricted"]:
        del body["branches"][a.branch]
    _show_access(ctx, a.project, ctx.client.request("PUT", path, body).data)


def teams_grants_set(ctx: Context) -> None:
    """`sudoo teams grants set`: give a team a level in a project or one of its branches.

    Args:
        ctx: The run context.
    """
    _change_grant(ctx, ctx.args.level)


def teams_grants_remove(ctx: Context) -> None:
    """`sudoo teams grants remove`: take a team's grant in a project or branch away.

    Args:
        ctx: The run context.
    """
    _change_grant(ctx, None)
