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
    if not sys.stdin.isatty():
        raise CliError(
            "Not a terminal: give the address with --url and the credentials in SUDOO_TOKEN and SUDOO_SECRET.", USAGE
        )
    value = (getpass.getpass(prompt) if secret else input(prompt)).strip()
    if not value:
        raise CliError("Nothing entered.", USAGE)
    return value


def logout(ctx: Context) -> None:
    name = config.profile_name(ctx.args.profile)
    existed = config.forget(name)
    if ctx.json:
        emit_json({"profile": name, "removed": existed})
    else:
        print(f"Profile {name!r} removed." if existed else f"There was no profile {name!r}.")


def profiles(ctx: Context) -> None:
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
    if ctx.json:
        emit_json({"version": __version__, "python": platform.python_version()})
    else:
        print(f"sudoo-cli {__version__} (Python {platform.python_version()})")


def status(ctx: Context) -> None:
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

PROJECT_COLUMNS = [
    ("id", get("id")),
    ("repository", get("repository.full_name")),
    ("odoo", lambda p: f"{get('odoo.version')(p) or '-'} {get('odoo.edition')(p) or ''}".strip()),
    ("branches", get("branches.total")),
    ("instances", get("instances.total")),
    ("attention", get("attention")),
]


def projects_list(ctx: Context) -> None:
    response = ctx.client.get("/projects")
    if ctx.json:
        emit_json(response.json())
    else:
        table(response.data or [], PROJECT_COLUMNS, "No projects yet.")


def projects_show(ctx: Context) -> None:
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
    a = ctx.args
    create: dict[str, Any] = {"owner": a.owner, "name": a.repo_name, "private": not a.public}
    if a.description:
        create["description"] = a.description
    body: dict[str, Any] = {"create": create, "odoo_version": a.odoo_version, "edition": a.edition}
    if a.name:
        body["name"] = a.name
    _project_made(ctx, ctx.client.post("/projects", body))


def projects_link(ctx: Context) -> None:
    a = ctx.args
    body: dict[str, Any] = {"repository": a.repository, "odoo_version": a.odoo_version, "edition": a.edition}
    if a.name:
        body["name"] = a.name
    _project_made(ctx, ctx.client.post("/projects", body))


def _project_made(ctx: Context, response: Any) -> None:
    if ctx.json:
        emit_json(response.json())
    else:
        p = response.data
        print(f"Project {p.get('id')} ({get('repository.full_name')(p)}), origin {p.get('origin')}.")


def _branches_table(branches: list[dict[str, Any]]) -> None:
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
    response = ctx.client.get(f"/projects/{quote(ctx.args.project)}", refresh=ctx.args.refresh or None)
    branches = response.data.get("branches_list") or []
    if ctx.json:
        emit_json({"data": branches, "meta": response.meta})
    else:
        _branches_table(branches)


def branches_set_stage(ctx: Context) -> None:
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
    data: dict[str, Any] = ctx.client.get(f"/instances/{quote(name)}").data
    return data


def instances_show(ctx: Context) -> None:
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
        body: dict[str, Any] = {}
        if getattr(ctx.args, "confirm_production", False):
            body["confirm_production"] = True
        ctx.job(ctx.client.post(f"/instances/{quote(ctx.args.instance)}/actions/{action}", body).data)

    return run


def instances_delete(ctx: Context) -> None:
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
    if isinstance(value, list):
        return [v if isinstance(v, str) else str(v.get("name")) for v in value if v]
    if isinstance(value, dict):
        return [str(n) for n in value.get("names") or []]
    if isinstance(value, str):
        return [value]
    return []


# -- backups ----------------------------------------------------------------------------------------------------------

BACKUP_COLUMNS = [
    ("file", get("file")),
    ("instance", get("instance")),
    ("kind", get("kind")),
    ("odoo", get("series")),
    ("size", lambda b: size(b.get("size", b.get("size_bytes")))),
    ("created", lambda b: when(b.get("created", b.get("created_at")))),
]
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
    ctx.job(ctx.client.post(f"/instances/{quote(ctx.args.instance)}/actions/backup", {}).data)


def backups_restore(ctx: Context) -> None:
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
    a = ctx.args
    body: dict[str, Any] = {}
    if a.public_key:
        body["public_key"] = _read_text(a.public_key)
    ctx.job(ctx.client.post(f"/offsite/copies/{quote(a.copy)}/verify", body).data)


def backups_offsite(ctx: Context) -> None:
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
    try:
        return Path(path).read_text()
    except OSError as error:
        raise CliError(f"Cannot read {path}: {error}", USAGE) from None


# -- domains ----------------------------------------------------------------------------------------------------------


def domains_list(ctx: Context) -> None:
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
    body = dict(extra)
    if getattr(ctx.args, "confirm_production", False):
        body["confirm_production"] = True
    return body


def _domain_path(ctx: Context, suffix: str = "") -> str:
    return f"/instances/{quote(ctx.args.instance)}/domains/{quote(ctx.args.domain)}{suffix}"


def _show_domain(ctx: Context, domain: dict[str, Any]) -> None:
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
    body = _domain_body(ctx, domain=ctx.args.domain)
    _show_domain(ctx, ctx.client.post(f"/instances/{quote(ctx.args.instance)}/domains", body).data)


def domains_remove(ctx: Context) -> None:
    ctx.confirm(f"Remove {ctx.args.domain} from {ctx.args.instance} (its certificate is deleted)?")
    ctx.client.request("DELETE", _domain_path(ctx), _domain_body(ctx) or None)
    if ctx.json:
        emit_json({"removed": ctx.args.domain})
    else:
        print(f"{ctx.args.domain} removed.")


def domains_status(ctx: Context) -> None:
    if ctx.args.check:
        domain = ctx.client.post(_domain_path(ctx, "/check")).data
    else:
        data = ctx.client.get(f"/instances/{quote(ctx.args.instance)}/domains").data
        domain = next((d for d in data.get("domains") or [] if d.get("domain") == ctx.args.domain), None)
        if domain is None:
            raise CliError(f"{ctx.args.instance} has no domain {ctx.args.domain}.", NOT_FOUND)
    _show_domain(ctx, domain)


def domains_primary(ctx: Context) -> None:
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
    a = ctx.args
    response = ctx.client.get("/jobs", instance=a.instance, limit=a.limit, offset=a.offset)
    if ctx.json:
        emit_json(response.json())
    else:
        table(response.data or [], JOB_COLUMNS, "No jobs.")


def jobs_show(ctx: Context) -> None:
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
    ctx.job(ctx.client.get(f"/jobs/{quote(ctx.args.job)}").data)


def jobs_log(ctx: Context) -> None:
    response = ctx.client.get(f"/jobs/{quote(ctx.args.job)}/log", offset=ctx.args.offset)
    if ctx.json:
        emit_json(response.json())
    else:
        sys.stdout.write(response.data.get("text", ""))


def users_list(ctx: Context) -> None:
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
    response = ctx.client.get("/roles")
    if ctx.json:
        emit_json(response.json())
        return
    table(
        response.data.get("roles") or [],
        [("role", get("name")), ("label", get("label")), ("description", get("description"))],
    )
