"""The `sudoo` command line: options, sub-commands and the exit code.

`build_parser` defines every command and option and connects each to a handler in commands.py. `main` parses
the arguments, runs the handler and turns errors into messages and exit codes.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from typing import Any

from . import __version__, commands, completion
from .context import Context
from .errors import ERROR, INTERRUPTED, OK, CliError
from .output import emit_json

# The stages of a branch or instance.
STAGES = ("development", "staging", "production")
# The Odoo editions.
EDITIONS = ("community", "enterprise")
# The statuses an instance can have, used to filter `instances list`.
STATUSES = ("creating", "online", "starting", "busy", "paused", "switched_off", "stopped", "unknown")

# Help text of the `teams` command group.
TEAMS_HELP = (
    "teams of accounts and their access to projects and branches. Teams are a premium feature (Pro and Business): "
    "on other plans the panel answers 403 when you create a team, add members or give access; reading, taking "
    "members out, taking access away and deleting always work"
)

# The text printed at the end of `sudoo --help`: where credentials come from, the exit codes and the documentation.
EPILOG = """\
Credentials come from `sudoo login` (a profile) or from SUDOO_URL, SUDOO_TOKEN and SUDOO_SECRET.
Exit codes: 0 ok, 1 error, 2 usage, 3 not logged in or credentials refused, 4 not allowed, 5 not found,
6 conflict, 7 invalid value, 8 rate limited, 9 panel error, 10 network or TLS, 11 job failed, 130 interrupted.
Documentation: https://docs.sudoo.sh/cli"""


def _globals(defaults: bool) -> argparse.ArgumentParser:
    """Options accepted before or after the sub-command (`sudoo --json instances list` = `sudoo instances list --json`).

    Args:
        defaults: True for the parser at the top, which sets the default values. False for the copy given to each
            sub-command, which sets none so that it does not overwrite a value given before the sub-command.

    Returns:
        A parser with the global options, to be used as a parent parser.
    """
    p = argparse.ArgumentParser(add_help=False)
    d: Any = None if defaults else argparse.SUPPRESS
    g = p.add_argument_group("global options")
    g.add_argument(
        "--profile",
        "-p",
        metavar="NAME",
        default=d,
        help="the profile to use (default: SUDOO_PROFILE or the default profile)",
    )
    g.add_argument("--json", action="store_true", default=False if defaults else d, help="print JSON for scripts")
    g.add_argument("--quiet", "-q", action="store_true", default=False if defaults else d, help="no progress lines")
    g.add_argument(
        "--debug",
        action="store_true",
        default=False if defaults else d,
        help="show requests on stderr (secrets redacted)",
    )
    g.add_argument("--ca-cert", metavar="FILE", default=d, help="trust this CA certificate (PEM) for the panel's TLS")
    g.add_argument("--acting-for", metavar="PERSON", default=d, help="X-Sudoo-Acting-For: who the call is made for")
    g.add_argument(
        "--timeout", type=float, metavar="SECONDS", default=60.0 if defaults else d, help="per request (default 60)"
    )
    return p


def _job_options(p: argparse.ArgumentParser) -> None:
    """Add the options of commands that start a job.

    Args:
        p: The parser of the command.
    """
    g = p.add_argument_group("job options")
    g.add_argument("--no-wait", action="store_true", help="return once the panel accepted the job")
    g.add_argument("--poll", action="store_true", help="follow the job by polling instead of the live event stream")
    g.add_argument("--wait-timeout", type=float, metavar="SECONDS", help="give up waiting after this long (exit 1)")


# A command handler: a function that takes the run `Context` and returns nothing.
Handler = Callable[[Context], None]


class Builder:
    """Helps to build the tree of commands.

    Attributes:
        common: Parent parser holding the global options, given to every command.
        root: The parser of the `sudoo` command.
        top: The sub-command group at the top level.
    """

    def __init__(self) -> None:
        """Create the root parser and its top-level sub-command group."""
        self.common = _globals(defaults=False)
        self.root = argparse.ArgumentParser(
            prog="sudoo",
            description="Manage a Sudoo.sh panel: projects, instances, deployments, backups, domains and jobs.",
            epilog=EPILOG,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            parents=[_globals(defaults=True)],
        )
        self.root.add_argument("--version", action="version", version=f"sudoo-cli {__version__}")
        self.top = self.root.add_subparsers(dest="command", metavar="COMMAND", required=True)

    def group(self, name: str, help_: str) -> Any:
        """Add a command group such as `projects`.

        Args:
            name: The group's name.
            help_: Its help text.

        Returns:
            The object to add the group's actions to.
        """
        p = self.top.add_parser(name, help=help_, description=help_)
        return p.add_subparsers(dest="subcommand", metavar="ACTION", required=True)

    def subgroup(self, parent: Any, name: str, help_: str) -> Any:
        """A group inside a group (`sudoo teams members add ...`).

        Args:
            parent: The sub-command object of the outer group.
            name: The inner group's name.
            help_: Its help text.

        Returns:
            The object to add the inner group's actions to.
        """
        p = parent.add_parser(name, help=help_, description=help_)
        return p.add_subparsers(dest="action", metavar="ACTION", required=True)

    def cmd(self, parent: Any, name: str, help_: str, handler: Handler, job: bool = False) -> argparse.ArgumentParser:
        """Add a command and connect it to its handler.

        Args:
            parent: The sub-command object to add it to.
            name: The command's name.
            help_: Its help text.
            handler: The function that runs it.
            job: True when the command starts a job; this adds --no-wait, --poll and --wait-timeout.

        Returns:
            The command's parser, so that the caller can add the arguments.
        """
        p: argparse.ArgumentParser = parent.add_parser(name, help=help_, description=help_, parents=[self.common])
        p.set_defaults(handler=handler)
        if job:
            _job_options(p)
        return p


def build_parser() -> argparse.ArgumentParser:
    """Define the whole command line.

    Returns:
        The parser of the `sudoo` command, with every command and option.
    """
    b = Builder()
    top = b.top

    p = b.cmd(top, "login", "store a panel's address, API token and signing secret in a profile", commands.login)
    p.add_argument("--url", help="the panel's address, https://...")
    p.add_argument("--default", action="store_true", help="make this the default profile")
    p.add_argument("--no-verify", action="store_true", help="do not check the credentials with the panel")
    b.cmd(top, "logout", "forget a profile and its credentials", commands.logout)
    b.cmd(top, "profiles", "list the profiles", commands.profiles)
    b.cmd(top, "status", "the panel's health, version, licence, usage and limits", commands.status)
    b.cmd(top, "version", "the version of this command", commands.version)

    # projects
    g = b.group("projects", "projects: one GitHub repository each")
    b.cmd(g, "list", "every project", commands.projects_list)
    p = b.cmd(g, "show", "a project and its branches", commands.projects_show)
    p.add_argument("project", help="the project's id")
    p.add_argument("--refresh", action="store_true", help="read the branches from GitHub now")
    p = b.cmd(g, "create", "create a new, empty GitHub repository and its project", commands.projects_create)
    p.add_argument("--owner", required=True, help="the GitHub organisation")
    p.add_argument("--repo-name", required=True, help="the new repository's name")
    p.add_argument("--public", action="store_true", help="a public repository (private by default)")
    p.add_argument("--description")
    _odoo_options(p)
    p.add_argument("--name", help="the project's name (default: the repository's)")
    p = b.cmd(g, "link", "make a project of an existing GitHub repository", commands.projects_link)
    p.add_argument("repository", help="https://github.com/owner/repo")
    _odoo_options(p)
    p.add_argument("--name", help="the project's name (default: the repository's)")

    # branches
    g = b.group("branches", "a project's branches and their stages")
    p = b.cmd(g, "list", "a project's branches", commands.branches_list)
    p.add_argument("project")
    p.add_argument("--refresh", action="store_true", help="read the branches from GitHub now")
    p = b.cmd(g, "set-stage", "move a branch (and its instances) to a stage", commands.branches_set_stage)
    p.add_argument("project")
    p.add_argument("branch")
    p.add_argument("stage", choices=STAGES)
    p.add_argument("--confirm-production", action="store_true", help="needed to move into or out of production")
    p = b.cmd(
        g,
        "create-instance",
        "an instance of a branch, in the branch's stage",
        commands.branches_create_instance,
        job=True,
    )
    p.add_argument("project")
    p.add_argument("branch")
    p.add_argument("--name")
    p.add_argument("--demo", action=argparse.BooleanOptionalAction, default=None, help="Odoo's demo data (development)")
    p.add_argument("--no-install-modules", action="store_true", help="do not install the repository's modules")

    # instances
    g = b.group("instances", "instances: list, create, operate, logs")
    p = b.cmd(g, "list", "every instance", commands.instances_list)
    p.add_argument("--project", help="only this project's")
    p.add_argument("--status", choices=STATUSES)
    p.add_argument("--stage", choices=STAGES)
    p = b.cmd(g, "show", "one instance", commands.instances_show)
    p.add_argument("instance")
    p = b.cmd(
        g, "create", "a new instance from a repository's branch, or plain Odoo", commands.instances_create, job=True
    )
    p.add_argument("--repo", help="https://github.com/owner/repo (none: plain Odoo)")
    p.add_argument("--branch")
    _odoo_options(p)
    p.add_argument("--name", help="the instance's name (default: made from the repository and branch)")
    p.add_argument("--stage", choices=STAGES, default="development")
    p.add_argument("--demo", action=argparse.BooleanOptionalAction, default=None, help="Odoo's demo data")
    p.add_argument("--no-install-modules", action="store_true")
    p.add_argument("--delete-after-days", type=int, metavar="DAYS", help="lifetime; 0 keeps it")
    p.add_argument("--from-backup", metavar="FILE", help="start from this backup")
    for name, action, help_ in (
        ("start", "start", "start a paused or stopped instance"),
        ("pause", "pause", "pause an instance (it starts again on its next visit)"),
        ("restart", "restart", "restart Odoo"),
        ("stop", "switch-off", "switch an instance off (it stays off until started)"),
    ):
        p = b.cmd(g, name, help_, commands.instance_action(action), job=True)
        p.add_argument("instance")
        if name in ("pause", "stop"):
            p.add_argument("--confirm-production", action="store_true", help="needed for a production instance")
    p = b.cmd(g, "delete", "delete an instance, after a backup", commands.instances_delete, job=True)
    p.add_argument("instance")
    p.add_argument("--yes", "-y", action="store_true", help="do not ask")
    p.add_argument("--confirm-production", action="store_true", help="needed for a production instance")
    p.add_argument("--without-backup", action="store_true", help="delete even when the backup fails (audit-logged)")
    p = b.cmd(g, "set", "change an instance's settings or stage", commands.instances_set)
    p.add_argument("instance")
    p.add_argument("--auto-update", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--auto-upgrade", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--auto-pause", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--delete-after-days", type=int, metavar="DAYS", help="0 keeps it")
    p.add_argument("--stage", choices=STAGES)
    p.add_argument("--confirm-production", action="store_true", help="needed to move out of production")
    p = b.cmd(g, "logs", "the end of Odoo's log", commands.instances_logs)
    p.add_argument("instance")
    p.add_argument("--lines", "-n", type=int, default=300, help="1 to 5000 (default 300)")
    p.add_argument("--follow", "-f", action="store_true", help="keep printing new lines")
    p.add_argument("--interval", type=float, default=3.0, help="seconds between reads with --follow")
    p = b.cmd(g, "open", "print an instance's address and open it in a browser", commands.instances_open)
    p.add_argument("instance")
    p.add_argument("--print", dest="print_only", action="store_true", help="only print the address")

    # deploy
    p = b.cmd(
        top,
        "deploy",
        "get the latest code of a branch into its instances and follow the job",
        commands.deploy,
        job=True,
    )
    p.add_argument("instance", nargs="?", help="the instance to update")
    p.add_argument("--project", help="with --branch: every instance of that branch")
    p.add_argument("--branch")
    p.add_argument("--submodules", action="store_true", help="update the submodules instead")

    # backups
    g = b.group("backups", "backups on the server and off-site copies")
    p = b.cmd(g, "list", "backups, newest first", commands.backups_list)
    p.add_argument("--instance", help="only this instance's")
    p.add_argument("--offsite", action="store_true", help="the off-site copies instead")
    p = b.cmd(g, "create", "back an instance up now", commands.backups_create, job=True)
    p.add_argument("instance")
    p = b.cmd(
        g,
        "restore",
        "restore a backup into an instance (a before-restore backup is taken first)",
        commands.backups_restore,
        job=True,
    )
    p.add_argument("instance")
    p.add_argument("--file", help="the backup file (default: the instance's latest)")
    p.add_argument("--offsite", metavar="COPY_ID", help="restore this off-site copy instead")
    p.add_argument("--public-key", metavar="PEM_FILE", help="check the off-site copy with this key")
    p.add_argument("--confirm-production", action="store_true", help="needed for a production instance")
    p.add_argument("--yes", "-y", action="store_true", help="do not ask")
    p = b.cmd(g, "download", "download a backup (zip)", commands.backups_download)
    p.add_argument("file")
    p.add_argument("--output", "-o", metavar="PATH", help="file or folder (default: the file's name, here)")
    p.add_argument("--force", action="store_true", help="replace an existing file")
    p = b.cmd(g, "verify", "download and check an off-site copy", commands.backups_verify, job=True)
    p.add_argument("copy", metavar="COPY_ID")
    p.add_argument("--public-key", metavar="PEM_FILE", help="a signing key kept off the server")
    b.cmd(g, "offsite", "the off-site copy settings and state", commands.backups_offsite)

    # domains
    g = b.group("domains", "an instance's custom domains")
    p = b.cmd(g, "list", "the domains and how to point DNS", commands.domains_list)
    p.add_argument("instance")
    for name, handler, help_ in (
        ("add", commands.domains_add, "add a domain"),
        ("remove", commands.domains_remove, "remove a domain and its certificate"),
        ("status", commands.domains_status, "one domain's state and DNS records"),
        ("primary", commands.domains_primary, "make a domain the primary one"),
    ):
        p = b.cmd(g, name, help_, handler)
        p.add_argument("instance")
        p.add_argument("domain")
        if name != "status":
            p.add_argument("--confirm-production", action="store_true", help="needed for a production instance")
        if name == "remove":
            p.add_argument("--yes", "-y", action="store_true", help="do not ask")
        if name == "status":
            p.add_argument("--check", action="store_true", help="check DNS (and issue) now")

    p = b.cmd(top, "shell", "a shell in an instance (the panel's web app only)", commands.shell)
    p.add_argument("instance")

    g = b.group("mail", "e-mails caught from staging and development instances")
    p = b.cmd(g, "list", "caught e-mails, newest first", commands.mail_list)
    p.add_argument("instance")
    p.add_argument("--search", "-s", help="words in the subject, addresses or text")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--offset", type=int, default=0)
    p = b.cmd(g, "show", "one caught e-mail", commands.mail_show)
    p.add_argument("instance")
    p.add_argument("id")

    g = b.group("resources", "an instance's workers, memory and CPU limits")
    p = b.cmd(g, "show", "what applies, and where it comes from", commands.resources_show)
    p.add_argument("instance")
    p = b.cmd(
        g, "set", "set limits: KEY=VALUE ..., VALUE 'default' follows the stage", commands.resources_set, job=True
    )
    p.add_argument("instance")
    p.add_argument("limits", nargs="+", metavar="KEY=VALUE", help=", ".join(commands.RESOURCE_KEYS))
    p.add_argument("--confirm-production", action="store_true", help="needed for a production instance")

    g = b.group("jobs", "background jobs")
    p = b.cmd(g, "list", "recent jobs, newest first", commands.jobs_list)
    p.add_argument("--instance")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--offset", type=int, default=0)
    p = b.cmd(g, "show", "one job", commands.jobs_show)
    p.add_argument("job")
    p.add_argument("--log", action="store_true", help="add its technical log")
    p = b.cmd(g, "follow", "follow a job until it ends (exit 11 when it failed)", commands.jobs_follow, job=True)
    p.add_argument("job")
    p = b.cmd(g, "log", "a job's technical log", commands.jobs_log)
    p.add_argument("job")
    p.add_argument("--offset", type=int, default=0)

    g = b.group("users", "the panel's accounts")
    b.cmd(g, "list", "every account (needs a write token)", commands.users_list)
    b.cmd(g, "roles", "the roles and their permissions", commands.roles_list)

    # teams (premium)
    g = b.group("teams", TEAMS_HELP)
    b.cmd(g, "list", "every team, with its members and the projects that name it", commands.teams_list)
    p = b.cmd(g, "show", "a team: its members and projects", commands.teams_show)
    p.add_argument("team", help="the team's id or name")
    p = b.cmd(g, "create", "make a team", commands.teams_create)
    p.add_argument("name")
    p.add_argument("--description")
    p.add_argument("--member", action="append", metavar="ACCOUNT", help="an account to add (repeat for more)")
    p = b.cmd(g, "rename", "give a team a new name (and description)", commands.teams_rename)
    p.add_argument("team", help="the team's id or name")
    p.add_argument("name", help="the new name")
    p.add_argument("--description")
    p = b.cmd(g, "delete", "delete a team; it leaves every grant and lock", commands.teams_delete)
    p.add_argument("team", help="the team's id or name")
    p.add_argument("--yes", "-y", action="store_true", help="do not ask")
    members = b.subgroup(g, "members", "add accounts to a team or take them out")
    for name, handler, help_ in (
        ("add", commands.teams_members_add, "add accounts to a team"),
        ("remove", commands.teams_members_remove, "take accounts out of a team"),
    ):
        p = b.cmd(members, name, help_, handler)
        p.add_argument("team", help="the team's id or name")
        p.add_argument("accounts", nargs="+", metavar="ACCOUNT", help="the panel account's name")
    grants = b.subgroup(g, "grants", "the levels teams have in projects and branches")
    p = b.cmd(grants, "list", "a project's grants (teams and accounts) and its branches'", commands.teams_grants_list)
    p.add_argument("project", help="the project's id")
    p = b.cmd(grants, "set", "give a team a level in a project or one of its branches", commands.teams_grants_set)
    p.add_argument("team", help="the team's id or name")
    p.add_argument("project", help="the project's id")
    p.add_argument("level", choices=commands.ACCESS_LEVELS)
    p.add_argument("--branch", help="only this branch")
    p = b.cmd(grants, "remove", "take a team's grant in a project or branch away", commands.teams_grants_remove)
    p.add_argument("team", help="the team's id or name")
    p.add_argument("project", help="the project's id")
    p.add_argument("--branch", help="only this branch")

    p = b.cmd(top, "completion", "print a shell completion script", lambda ctx: completion.run(ctx, b.root))
    p.add_argument("shell", choices=("bash", "zsh", "fish"))
    return b.root


def _odoo_options(p: argparse.ArgumentParser) -> None:
    """Add the options that choose an Odoo version and edition.

    Args:
        p: The parser of the command.
    """
    p.add_argument(
        "--odoo-version", "--version", dest="odoo_version", required=True, metavar="VERSION", help="19.0, 18.0, ..."
    )
    p.add_argument("--edition", choices=EDITIONS, default="community")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command.

    Args:
        argv: The arguments, without the program name; sys.argv is used when None.

    Returns:
        The exit code. Errors are printed to stderr (and as JSON with --json) and mapped to their exit code; Ctrl+C
        gives INTERRUPTED; a closed pipe is not an error; an unexpected error gives ERROR and is shown without a
        traceback unless --debug is given.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    ctx = Context(args)
    try:
        handler: Callable[[Context], None] = args.handler
        handler(ctx)
        return OK
    except CliError as error:
        if ctx.json:
            emit_json(error.as_json())
        print(f"sudoo: {error.message}", file=sys.stderr)
        return error.code
    except KeyboardInterrupt:
        print("", file=sys.stderr)
        return INTERRUPTED
    except BrokenPipeError:
        return OK
    except Exception as error:  # an unexpected answer: say what, without a traceback full of internals
        if getattr(args, "debug", False):
            raise
        print(f"sudoo: unexpected error: {type(error).__name__}: {error}. Run again with --debug.", file=sys.stderr)
        return ERROR


if __name__ == "__main__":
    sys.exit(main())
