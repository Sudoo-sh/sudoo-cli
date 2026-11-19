"""Every command group against the mock panel."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sudoo_cli import __version__, errors

from .conftest import Run
from .mock_panel import MockPanel


def test_version(run: Run) -> None:
    result = run("version", "--json")
    assert result.code == 0
    assert result.json()["version"] == __version__ == "3.0.1"


def test_status(run: Run) -> None:
    result = run("status")
    assert result.code == 0
    assert "3.0.0" in result.out
    assert "2 running of 3" in result.out
    data = run("status", "--json").json()
    assert data["healthy"] is True
    assert data["capacity"] is None  # 503 before the first measurement is not an error


def test_global_options_before_or_after_the_command(run: Run) -> None:
    assert json.loads(run("--json", "instances", "list").out) == json.loads(run("instances", "list", "--json").out)


def test_projects(run: Run, panel: MockPanel) -> None:
    result = run("projects", "list")
    assert "acme/sales" in result.out
    assert run("projects", "show", "sales").code == 0
    assert run("projects", "show", "nope").code == errors.NOT_FOUND
    linked = run("projects", "link", "https://github.com/acme/erp", "--odoo-version", "19.0", "--json")
    assert linked.json()["data"]["origin"] == "linked"
    run("projects", "create", "--owner", "acme", "--repo-name", "fresh", "--version", "19.0")
    body = json.loads(panel.state.requests[-1]["body"])
    assert body["create"] == {"owner": "acme", "name": "fresh", "private": True}
    assert body["odoo_version"] == "19.0"


def test_branches(run: Run, panel: MockPanel) -> None:
    result = run("branches", "list", "sales")
    assert "feature/x" in result.out and "(default)" in result.out
    assert run("branches", "set-stage", "sales", "feature/x", "production").code == errors.INVALID
    assert run("branches", "set-stage", "sales", "feature/x", "production", "--confirm-production").code == 0
    assert panel.state.requests[-1]["path"] == "/api/v1/projects/sales/branches/feature/x/stage"
    created = run("branches", "create-instance", "sales", "feature/x", "--name", "sales-x", "--no-demo")
    assert created.code == 0
    creation = next(r for r in panel.state.requests if r["path"].endswith("/instances") and r["method"] == "POST")
    assert creation["path"] == "/api/v1/projects/sales/branches/feature/x/instances"
    assert json.loads(creation["body"]) == {"name": "sales-x", "demo": False}


def test_instances_list_show_and_filters(run: Run) -> None:
    listed = run("instances", "list", "--json").json()
    assert {i["name"] for i in listed} == {"sales-main", "sales-dev", "shop"}
    staging = run("instances", "list", "--stage", "staging", "--json").json()
    assert [i["name"] for i in staging] == ["shop"]
    shown = run("instances", "show", "sales-main")
    assert "production" in shown.out and "https://sales-main.example.com" in shown.out
    assert run("instances", "show", "ghost").code == errors.NOT_FOUND


def test_instances_create_follows_the_job(run: Run, panel: MockPanel) -> None:
    result = run(
        "instances",
        "create",
        "--repo",
        "https://github.com/acme/sales",
        "--branch",
        "main",
        "--version",
        "19.0",
        "--name",
        "ci-42",
        "--delete-after-days",
        "7",
        "--json",
    )
    assert result.code == 0
    assert result.json()["status"] == "success"
    body = json.loads(next(r["body"] for r in panel.state.requests if r["method"] == "POST"))
    assert body == {
        "version": "19.0",
        "edition": "community",
        "stage": "development",
        "repo": "https://github.com/acme/sales",
        "branch": "main",
        "name": "ci-42",
        "delete_after_days": 7,
    }


def test_a_refused_value_shows_the_problem_detail(run: Run) -> None:
    result = run("instances", "create", "--version", "12.0")
    assert result.code == errors.INVALID
    assert "Choose an Odoo version from the list." in result.err
    assert "req-123" in result.err


@pytest.mark.parametrize("command", ["start", "restart", "pause", "stop"])
def test_instance_actions(run: Run, panel: MockPanel, command: str) -> None:
    assert run("instances", command, "sales-dev").code == 0
    action = {"stop": "switch-off"}.get(command, command)
    assert any(r["path"] == f"/api/v1/instances/sales-dev/actions/{action}" for r in panel.state.requests)


def test_production_needs_confirmation(run: Run) -> None:
    assert run("instances", "pause", "sales-main").code == errors.INVALID
    assert run("instances", "pause", "sales-main", "--confirm-production").code == 0


def test_delete_asks_and_sends_the_confirmation(run: Run, panel: MockPanel) -> None:
    assert run("instances", "delete", "sales-dev").code == errors.USAGE  # not a terminal and no --yes
    assert run("instances", "delete", "sales-dev", "--yes").code == 0
    deletion = next(r for r in panel.state.requests if r["method"] == "DELETE")
    assert json.loads(deletion["body"]) == {"confirm": "sales-dev"}
    assert run("instances", "delete", "sales-main", "--yes").code == errors.INVALID
    assert run("instances", "delete", "sales-main", "--yes", "--confirm-production").code == 0


def test_instances_set(run: Run, panel: MockPanel) -> None:
    assert run("instances", "set", "sales-dev").code == errors.USAGE
    assert run("instances", "set", "sales-dev", "--delete-after-days", "14", "--no-auto-pause").code == 0
    assert json.loads(panel.state.requests[-1]["body"]) == {"auto_pause": False, "delete_after_days": 14}


def test_logs_and_follow(run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    result = run("instances", "logs", "sales-dev", "-n", "3")
    assert result.out.count("line") == 3
    calls = {"n": 0}

    def stop_after_two(seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] > 2:
            raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", stop_after_two)
    followed = run("instances", "logs", "sales-dev", "--follow")
    assert followed.code == errors.INTERRUPTED
    assert followed.out.count("line") > 3


def test_new_lines_of_a_moving_tail() -> None:
    from sudoo_cli.commands import new_lines

    assert new_lines(["a", "b", "c"], ["b", "c", "d", "e"]) == ["d", "e"]
    assert new_lines(["a", "b"], ["a", "b"]) == []
    assert new_lines(["a"], ["x", "y"]) == ["x", "y"]


def test_open_prints_the_address(run: Run) -> None:
    result = run("instances", "open", "sales-dev", "--print")
    assert result.out.strip() == "https://sales-dev.example.com"


def test_deploy_an_instance_and_a_branch(run: Run, panel: MockPanel) -> None:
    assert run("deploy", "sales-dev").code == 0
    assert run("deploy", "--project", "sales", "--branch", "feature/x").code == 0
    updates = [r["path"] for r in panel.state.requests if r["path"].endswith("/actions/update")]
    assert updates == ["/api/v1/instances/sales-dev/actions/update"] * 2
    assert run("deploy", "--project", "sales", "--branch", "gone").code == errors.NOT_FOUND
    assert run("deploy").code == errors.USAGE


def test_backups(run: Run, panel: MockPanel, tmp_path: Path) -> None:
    listed = run("backups", "list", "--instance", "shop", "--json").json()
    assert [b["instance"] for b in listed] == ["shop"]
    assert "c1" in run("backups", "list", "--offsite").out
    assert run("backups", "create", "sales-dev").code == 0
    assert run("backups", "restore", "sales-dev", "--yes", "--file", "x.zip").code == 0
    restore = next(r for r in panel.state.requests if r["path"].endswith("sales-dev/actions/restore"))
    assert json.loads(restore["body"]) == {"file": "x.zip"}
    assert run("backups", "restore", "sales-main", "--yes").code == errors.INVALID
    assert run("backups", "restore", "sales-main", "--yes", "--offsite", "c1", "--confirm-production").code == 0
    assert run("backups", "verify", "c1").code == 0
    assert run("backups", "offsite").code == 0
    target = tmp_path / "b.zip"
    result = run("backups", "download", "a.zip", "-o", str(target))
    assert result.code == 0 and target.stat().st_size == 200_002
    assert run("backups", "download", "a.zip", "-o", str(target)).code == errors.USAGE  # exists
    assert run("backups", "download", "../a.zip").code == errors.USAGE
    assert run("backups", "download", "missing.tar", "-o", str(tmp_path)).code == errors.NOT_FOUND
    assert not list(tmp_path.glob(".sudoo-*"))  # no partial file left behind


def test_domains(run: Run, panel: MockPanel) -> None:
    assert "erp.acme.com" in run("domains", "list", "sales-main").out
    assert run("domains", "add", "sales-dev", "shop.acme.com").code == 0
    status = run("domains", "status", "sales-dev", "shop.acme.com", "--json").json()
    assert status["status"] == "waiting_dns"
    assert run("domains", "status", "sales-dev", "shop.acme.com", "--check", "--json").json()["status"] == "active"
    assert run("domains", "remove", "sales-dev", "shop.acme.com", "--yes").code == 0
    assert run("domains", "status", "sales-dev", "shop.acme.com").code == errors.NOT_FOUND


def test_shell_points_to_the_panel(run: Run, panel: MockPanel) -> None:
    result = run("shell", "sales-dev")
    assert result.code == errors.FORBIDDEN
    assert f"{panel.url}/i/sales-dev/shell" in result.err


def test_mail(run: Run) -> None:
    assert "Your order" in run("mail", "list", "sales-dev").out
    assert run("mail", "list", "sales-main").code == errors.CONFLICT
    assert "Thank you." in run("mail", "show", "sales-dev", "m1").out


def test_resources(run: Run, panel: MockPanel) -> None:
    assert "odoo_workers" in run("resources", "show", "sales-dev").out
    assert run("resources", "set", "sales-dev", "odoo_workers=4", "odoo_memory_limit=4g").code == 0
    assert panel.state.own_resources["sales-dev"] == {"odoo_workers": 4, "odoo_memory_limit": "4g"}
    assert run("resources", "set", "sales-dev", "odoo_workers=default").code == 0
    assert panel.state.own_resources["sales-dev"] == {"odoo_memory_limit": "4g"}
    assert run("resources", "set", "sales-dev", "bogus=1").code == errors.USAGE


def test_jobs(run: Run) -> None:
    started = run("instances", "restart", "sales-dev", "--no-wait", "--json").json()
    assert started["status"] == "queued"
    assert started["id"] in run("jobs", "list").out
    assert run("jobs", "follow", started["id"]).code == 0
    assert "Restart" in run("jobs", "show", started["id"]).out
    assert "step two" in run("jobs", "log", started["id"]).out
    assert run("jobs", "show", "nope").code == errors.NOT_FOUND


def test_users_and_roles(run: Run) -> None:
    assert "sam@example.com" in run("users", "list").out
    assert "Administrator" in run("users", "roles").out


def test_teams(run: Run, panel: MockPanel) -> None:
    assert "No teams." in run("teams", "list").out
    made = run("teams", "create", "Sales devs", "--description", "The sales team", "--member", "sam", "--json")
    assert made.code == 0 and made.json()["id"] == "sales-devs"
    assert run("teams", "create", "sales DEVS").code == errors.CONFLICT
    assert run("teams", "create", "Other", "--member", "nobody").code == errors.INVALID
    assert "Sales devs" in run("teams", "list").out
    shown = run("teams", "show", "sales devs")  # by name, case aside
    assert shown.code == 0 and "Sam Lee" in shown.out
    assert run("teams", "show", "nope").code == errors.NOT_FOUND

    added = run("teams", "members", "add", "sales-devs", "ana", "omar", "--json").json()
    assert [m["name"] for m in added["members"]] == ["ana", "omar", "sam"]
    assert json.loads(panel.state.requests[-1]["body"]) == {"members": ["ana", "omar", "sam"]}
    removed = run("teams", "members", "remove", "sales-devs", "omar", "--json").json()
    assert [m["name"] for m in removed["members"]] == ["ana", "sam"]
    assert run("teams", "members", "remove", "sales-devs", "omar").code == errors.NOT_FOUND
    renamed = run("teams", "rename", "sales-devs", "Sales", "--json").json()
    assert renamed["name"] == "Sales" and renamed["id"] == "sales-devs"

    assert run("teams", "grants", "set", "Sales", "sales", "developer").code == 0
    assert run("teams", "grants", "set", "Sales", "sales", "admin", "--branch", "main").code == 0
    access = run("teams", "grants", "list", "sales", "--json").json()
    assert access["grants"] == [{"type": "team", "id": "sales-devs", "level": "developer", "name": "Sales"}]
    assert access["branches"]["main"]["grants"][0]["level"] == "admin"
    # a second grant replaces the first; the names the panel adds are never sent back
    assert run("teams", "grants", "set", "Sales", "sales", "view").code == 0
    sent = json.loads(panel.state.requests[-1]["body"])
    assert sent["grants"] == [{"type": "team", "id": "sales-devs", "level": "view"}]
    assert "branch main" in run("teams", "grants", "list", "sales").out
    assert "sales" in run("teams", "show", "Sales").out
    assert run("teams", "grants", "remove", "Sales", "sales", "--branch", "main").code == 0
    assert run("teams", "grants", "list", "sales", "--json").json()["branches"] == {}
    assert run("teams", "grants", "remove", "Sales", "sales", "--branch", "main").code == errors.NOT_FOUND
    assert run("teams", "grants", "set", "Sales", "nope", "view").code == errors.NOT_FOUND
    with pytest.raises(SystemExit) as usage:
        run("teams", "grants", "set", "Sales", "sales", "owner")
    assert usage.value.code == errors.USAGE

    # without the premium feature the panel refuses new teams and members (exit 4); taking out still works
    panel.state.teams_on_plan = False
    refused = run("teams", "create", "QA", "--json")
    assert refused.code == errors.FORBIDDEN and refused.json()["error"]["status"] == 403
    assert run("teams", "members", "add", "Sales", "omar").code == errors.FORBIDDEN
    assert run("teams", "members", "remove", "Sales", "ana").code == 0

    assert run("teams", "delete", "Sales").code == errors.USAGE  # not a terminal: needs --yes
    assert run("teams", "delete", "Sales", "--yes").code == 0
    assert run("teams", "list", "--json").json()["data"] == []


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_completion(run: Run, shell: str) -> None:
    script = run("completion", shell).out
    assert "instances" in script and "set-stage" in script


def test_the_zipapp_runs(tmp_path: Path) -> None:
    import subprocess
    import sys

    from scripts.build_zipapp import build

    archive = build(tmp_path / "sudoo.pyz")
    command = [sys.executable, "-I", str(archive), "version"]
    result = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603
    assert result.returncode == 0
    assert result.stdout.startswith(f"sudoo-cli {__version__}")
