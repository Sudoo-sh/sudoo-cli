"""A small stand-in for a Sudoo.sh panel's /api/v1, for the tests.

It checks every request's signature exactly as the panel does (token, timestamp window, one-time nonce, HMAC over
the five or six lines), answers in the panel's shapes (envelopes, problem details, 202 + Location for jobs), runs
jobs through queued -> running -> success, and serves them on a Server-Sent Events stream.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

TOKEN = "sudoo_test_token_0123456789"
SECRET = "test-signing-secret-abcdef"
READ_TOKEN = "sudoo_read_token_0123456789"
NONCE = re.compile(r"[A-Za-z0-9_-]{16,64}")
ACTING_FOR = re.compile(r"[A-Za-z0-9._@+\-]{1,120}")


class Problem(Exception):
    def __init__(self, status: int, detail: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(detail)
        self.status, self.detail, self.headers = status, detail, headers or {}


def instance(
    name: str, stage: str = "development", status: str = "online", project: str | None = "sales"
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "stage": stage,
        "project": {"id": project, "name": project} if project else None,
        "url": f"https://{name}.example.com",
        "odoo": {"version": "19.0", "edition": "community", "database": name.replace("-", "_")},
        "source": "repository",
        "git": {
            "repository": "https://github.com/acme/sales",
            "branch": "main",
            "commit": {"sha": "4f2a9c1", "subject": "Add delivery report"},
        },
        "settings": {"auto_update": True, "auto_upgrade": True, "auto_pause": True, "delete_after_days": 30},
        "usage": {
            "cpu_percent": 3.2,
            "memory_bytes": 905969664,
            "memory_limit_bytes": 3011510272,
            "disk_bytes": 412090368,
        },
        "created_at": "2026-11-11T10:16:47Z",
        "current_job": None,
        "update_failure": None,
        "backups": [
            {
                "file": f"{name}__db__20261112-000012__daily.zip",
                "kind": "daily",
                "size_bytes": 1000,
                "created_at": "2026-11-12T00:00:12Z",
            }
        ],
        "links": {"self": f"/api/v1/instances/{name}"},
    }


@dataclass
class State:
    instances: dict[str, dict[str, Any]] = field(default_factory=dict)
    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    requests: list[dict[str, Any]] = field(default_factory=list)
    nonces: set[str] = field(default_factory=set)
    domains: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    own_resources: dict[str, dict[str, Any]] = field(default_factory=dict)
    rate_limit_next: int = 0  # answer this many coming requests with 429
    events_status: int | None = None  # refuse the event stream with this status
    fail_jobs: bool = False
    counter: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def new_job(self, name: str | None, action: str) -> dict[str, Any]:
        with self.lock:
            self.counter += 1
            job_id = f"job{self.counter:09d}"
        job = {
            "id": job_id,
            "instance": name,
            "action": action,
            "status": "queued",
            "progress_percent": 0,
            "requested_by": "api:CI",
            "started_at": "2026-11-12T08:31:02Z",
            "finished_at": None,
            "steps": [{"title": action, "status": "queued"}],
            "notes": [],
            "error": None,
            "links": {"self": f"/api/v1/jobs/{job_id}"},
        }
        self.jobs[job_id] = job
        return job

    def advance(self, job: dict[str, Any]) -> dict[str, Any]:
        """queued -> running (50 %) -> success, or failed when fail_jobs."""
        if job["status"] == "queued":
            job.update(status="running", progress_percent=50)
            job["steps"][0]["status"] = "running"
        elif job["status"] == "running":
            failed = self.fail_jobs
            job.update(
                status="failed" if failed else "success", progress_percent=100, finished_at="2026-11-12T08:32:00Z"
            )
            job["steps"][0]["status"] = "failed" if failed else "done"
            job["error"] = "Odoo did not start." if failed else None
            if not failed and job["action"] == "Delete":
                self.instances.pop(job["instance"], None)
        return job


def default_state() -> State:
    state = State()
    for name, stage in (("sales-main", "production"), ("sales-dev", "development"), ("shop", "staging")):
        state.instances[name] = instance(name, stage, project="shop" if name == "shop" else "sales")
    state.domains["sales-main"] = [
        {
            "domain": "erp.acme.com",
            "primary": True,
            "status": "active",
            "reason": None,
            "dns_records": [{"type": "CNAME", "name": "erp.acme.com", "value": "panel.example.com"}],
            "certificate": {"expires_at": "2027-02-01T00:00:00Z"},
            "url": "https://erp.acme.com",
        }
    ]
    return state


class Handler(BaseHTTPRequestHandler):
    server: MockPanel
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    # -- plumbing --------------------------------------------------------------------------------------------------

    def _send(
        self,
        status: int,
        payload: Any = None,
        headers: dict[str, str] | None = None,
        raw: bytes | None = None,
        ctype: str = "application/json",
    ) -> None:
        body = raw if raw is not None else (b"" if payload is None else json.dumps(payload).encode())
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-ID", self.headers.get("X-Request-ID") or "srv-1")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _problem(self, status: int, detail: str, headers: dict[str, str] | None = None) -> None:
        titles = {
            400: "Bad Request",
            401: "Unauthorized",
            403: "Forbidden",
            404: "Not Found",
            409: "Conflict",
            429: "Too Many Requests",
        }
        problem = {
            "type": "about:blank",
            "title": titles.get(status, "Error"),
            "status": status,
            "detail": detail,
            "instance": self.path,
            "request_id": "req-123",
        }
        self._send(status, problem, headers, ctype="application/problem+json")

    def _verify(self, method: str, body: bytes) -> str:
        """The caller's scope, after checking the signature like the panel."""
        auth = self.headers.get("Authorization", "")
        tokens = {TOKEN: "write", READ_TOKEN: "read"}
        token = auth.removeprefix("Bearer ")
        if not auth.startswith("Bearer ") or token not in tokens:
            raise Problem(401, "The API token is not valid.")
        timestamp = self.headers.get("X-Sudoo-Timestamp", "")
        nonce = self.headers.get("X-Sudoo-Nonce", "")
        if not timestamp.isdigit() or abs(time.time() - int(timestamp)) > 300:
            raise Problem(401, "The timestamp is missing or more than 5 minutes away.")
        if not NONCE.fullmatch(nonce) or nonce in self.server.state.nonces:
            raise Problem(401, "The nonce is missing or was used already.")
        lines = [timestamp, nonce, method, self.path, hashlib.sha256(body).hexdigest()]
        acting = self.headers.get("X-Sudoo-Acting-For")
        if acting is not None:
            if not ACTING_FOR.fullmatch(acting):
                raise Problem(400, "X-Sudoo-Acting-For is not valid.")
            lines.append(acting)
        expected = "sha256=" + hmac.new(SECRET.encode(), "\n".join(lines).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, self.headers.get("X-Sudoo-Signature", "")):
            raise Problem(401, "The signature does not match.")
        self.server.state.nonces.add(nonce)
        return tokens[token]

    def _handle(self, method: str) -> None:
        state = self.server.state
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        url = urlsplit(self.path)
        if url.path == "/healthz":
            self._send(200, {"ok": True})
            return
        state.requests.append(
            {"method": method, "path": self.path, "body": body, "headers": dict(self.headers.items())}
        )
        try:
            if state.rate_limit_next > 0:
                state.rate_limit_next -= 1
                raise Problem(429, "Too many requests.", {"Retry-After": "0"})
            scope = self._verify(method, body)
            if method != "GET" and scope == "read":
                raise Problem(403, "This token may only read.")
            if body and self.headers.get("Content-Type") != "application/json":
                raise Problem(415, "Send JSON.")
            data = json.loads(body) if body else None
            route(self, method, url.path.removeprefix("/api/v1"), parse_qs(url.query), data)
        except Problem as problem:
            self._problem(problem.status, problem.detail, problem.headers)

    def ok(
        self, data: Any, meta: dict[str, Any] | None = None, status: int = 200, headers: dict[str, str] | None = None
    ) -> None:
        payload: dict[str, Any] = {"data": data}
        if meta is not None:
            payload["meta"] = meta
        self._send(status, payload, headers)

    def job(self, job: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        self.ok(job, status=202, headers={"Location": f"/api/v1/jobs/{job['id']}", **(headers or {})})

    def events(self) -> None:
        state = self.server.state
        if state.events_status:
            raise Problem(state.events_status, "The event stream is not for this token.")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b": hello\n\n")
        self.wfile.flush()
        threading.Event().wait(0.2)  # let the client read the job once first (time.sleep is patched out)
        number = 100
        for job in list(state.jobs.values()):
            while job["status"] not in ("success", "failed"):
                state.advance(job)
                number += 1
                frame = f"id: {number}\nevent: job\ndata: {json.dumps({'key': job['id'], 'data': job})}\n\n"
                self.wfile.write(b'event: metrics\ndata: {"key": "", "data": {}}\n\n')
                self.wfile.write(frame.encode())
                self.wfile.flush()
        self.close_connection = True


def route(h: Handler, method: str, path: str, query: dict[str, list[str]], body: Any) -> None:
    state = h.server.state
    q = {k: v[0] for k, v in query.items()}
    parts = [unquote(p) for p in path.strip("/").split("/")]

    def need_instance(name: str) -> dict[str, Any]:
        if name not in state.instances:
            raise Problem(404, f"There is no instance {name}.")
        return state.instances[name]

    match (method, parts):
        case ("GET", ["versions"]):
            h.ok([{"version": "19.0", "enterprise": True}])
        case ("GET", ["server"]):
            h.ok(
                {
                    "version": "3.0.0",
                    "panel_url": "https://panel.example.com",
                    "plugins": {"exposure": "host-ports"},
                    "metrics": {
                        "instances_running": 2,
                        "instances_total": 3,
                        "cpu_percent": 12.5,
                        "memory_percent": 40.0,
                        "disk_percent": 30.0,
                    },
                }
            )
        case ("GET", ["instance-quota"]):
            h.ok(
                {
                    "instances": 3,
                    "max_instances": 10,
                    "own_instances": 3,
                    "max_instances_per_user": 0,
                    "next_creation_at": None,
                }
            )
        case ("GET", ["server", "capacity"]):
            raise Problem(503, "No measurement yet.")
        case ("GET", ["licence"]):
            h.ok({"bundled_version": "1.0", "accepted": {"version": "1.0", "use": "company"}})
        case ("GET", ["instances"]):
            items = list(state.instances.values())
            limit, offset = int(q.get("limit", 100)), int(q.get("offset", 0))
            h.ok(
                items[offset : offset + limit],
                {"total": len(items), "count": len(items[offset : offset + limit]), "limit": limit, "offset": offset},
            )
        case ("POST", ["instances"]):
            if body.get("version") != "19.0":
                raise Problem(400, "Choose an Odoo version from the list.")
            name = body.get("name") or "new-instance"
            state.instances[name] = instance(name, body.get("stage", "development"), status="creating")
            h.job(state.new_job(name, "Create instance"), {"Content-Location": f"/api/v1/instances/{name}"})
        case ("GET", ["instances", name]):
            h.ok(need_instance(name))
        case ("PATCH", ["instances", name]):
            inst = need_instance(name)
            if (
                inst["stage"] == "production"
                and body.get("stage") not in (None, "production")
                and not body.get("confirm_production")
            ):
                raise Problem(400, "Moving an instance out of production needs confirm_production.")
            for key in ("auto_update", "auto_upgrade", "auto_pause", "delete_after_days"):
                if key in body:
                    inst["settings"][key] = body[key]
            if "stage" in body:
                inst["stage"] = body["stage"]
            h.ok(inst)
        case ("DELETE", ["instances", name]):
            inst = need_instance(name)
            if body.get("confirm") != name:
                raise Problem(400, "Type the instance's name to confirm.")
            if inst["stage"] == "production" and body.get("confirm_stage") != "production":
                raise Problem(400, "A production instance also needs confirm_stage.")
            h.job(state.new_job(name, "Delete"))
        case ("POST", ["instances", name, "actions", action]):
            inst = need_instance(name)
            if inst["status"] == "creating":
                raise Problem(409, f"{name} is being created.")
            if (
                action in ("pause", "switch-off", "restore")
                and inst["stage"] == "production"
                and not body.get("confirm_production")
            ):
                raise Problem(400, "This is a production instance: confirm with confirm_production.")
            labels = {
                "update": "Get latest code",
                "restart": "Restart",
                "backup": "Back up now",
                "restore": "Restore a backup",
                "start": "Start",
                "pause": "Pause",
                "switch-off": "Switch off",
                "update-submodules": "Update submodules",
            }
            if action not in labels:
                raise Problem(404, f"There is no action {action}.")
            h.job(state.new_job(name, labels[action]))
        case ("GET", ["instances", name, "logs"]):
            need_instance(name)
            count = len(state.requests)
            lines = [f"line {i}" for i in range(count, count + 3)]
            h.ok({"text": "\n".join(lines) + "\n"})
        case ("GET", ["instances", name, "resources"]):
            need_instance(name)
            own = state.own_resources.get(name, {"odoo_workers": 2})
            h.ok(
                {
                    "stage": "development",
                    "priority": {"rank": 3},
                    "limits": {
                        "odoo_workers": own.get("odoo_workers", 2),
                        "odoo_memory_limit": own.get("odoo_memory_limit", "2g"),
                    },
                    "own": own,
                    "stage_defaults": {"odoo_workers": 2},
                    "pending": False,
                    "warnings": [],
                }
            )
        case ("PUT", ["instances", name, "resources"]):
            need_instance(name)
            state.own_resources[name] = {k: v for k, v in body["limits"].items() if v is not None}
            h.job(state.new_job(name, "Apply resources"))
        case ("GET", ["instances", name, "domains"]):
            need_instance(name)
            h.ok(
                {
                    "mode": "acme",
                    "limit": 5,
                    "default_url": f"https://{name}.example.com",
                    "target": {"host": "panel.example.com", "record_type": "CNAME"},
                    "domains": state.domains.get(name, []),
                }
            )
        case ("POST", ["instances", name, "domains"]):
            inst = need_instance(name)
            if inst["stage"] == "production" and not body.get("confirm_production"):
                raise Problem(400, "Confirm the production instance.")
            domain = {
                "domain": body["domain"],
                "primary": False,
                "status": "waiting_dns",
                "reason": "No DNS record yet.",
                "dns_records": [{"type": "CNAME", "name": body["domain"], "value": "panel.example.com"}],
            }
            state.domains.setdefault(name, []).append(domain)
            h.ok(domain, status=201, headers={"Location": f"/api/v1/instances/{name}/domains/{body['domain']}"})
        case ("POST", ["instances", name, "domains", domain, "check"]):
            found = next((d for d in state.domains.get(name, []) if d["domain"] == domain), None)
            if not found:
                raise Problem(404, "No such domain.")
            found["status"] = "active"
            h.ok(found)
        case ("DELETE", ["instances", name, "domains", domain]):
            state.domains[name] = [d for d in state.domains.get(name, []) if d["domain"] != domain]
            h._send(204)
        case ("GET", ["instances", name, "mail"]):
            inst = need_instance(name)
            if inst["stage"] == "production":
                raise Problem(409, "Production instances send their e-mail.")
            mails = [
                {
                    "id": "m1",
                    "received_at": 1794430123,
                    "subject": "Your order",
                    "from": "shop@acme.com",
                    "to": ["sam@example.com"],
                }
            ]
            if q.get("q") and q["q"] not in "Your order":
                mails = []
            h.ok(mails, {"total": len(mails), "count": len(mails), "limit": 50, "offset": 0})
        case ("GET", ["instances", name, "mail", mail_id]):
            h.ok(
                {
                    "id": mail_id,
                    "subject": "Your order",
                    "from": "shop@acme.com",
                    "to": ["sam@example.com"],
                    "text": "Thank you.",
                    "attachment_list": [],
                }
            )
        case ("GET", ["projects"]):
            h.ok(
                [
                    {
                        "id": "sales",
                        "name": "sales",
                        "repository": {"full_name": "acme/sales", "url": "https://github.com/acme/sales"},
                        "odoo": {"version": "19.0", "edition": "community"},
                        "branches": {"total": 2},
                        "instances": {"total": 2, "names": ["sales-main", "sales-dev"]},
                        "attention": 0,
                    }
                ]
            )
        case ("GET", ["projects", project_id]):
            if project_id != "sales":
                raise Problem(404, "No such project.")
            h.ok(
                {
                    "id": "sales",
                    "name": "sales",
                    "repository": {"full_name": "acme/sales", "url": "https://github.com/acme/sales"},
                    "odoo": {"version": "19.0", "edition": "community"},
                    "origin": "linked",
                    "instances": {"names": ["sales-main", "sales-dev"]},
                    "branches_list": [
                        {
                            "name": "main",
                            "stage": "production",
                            "default": True,
                            "on_github": True,
                            "commit": {"sha": "4f2a9c1", "subject": "Add delivery report"},
                            "instances": ["sales-main"],
                        },
                        {
                            "name": "feature/x",
                            "stage": "development",
                            "default": False,
                            "on_github": True,
                            "commit": {"sha": "9b1e0c4", "subject": "WIP"},
                            "instances": ["sales-dev"],
                        },
                    ],
                },
                {"default_branch": "main"},
            )
        case ("POST", ["projects"]):
            if "create" in body:
                h.ok(
                    {
                        "id": body["create"]["name"],
                        "repository": {"full_name": f"{body['create']['owner']}/{body['create']['name']}"},
                        "origin": "created",
                    },
                    status=201,
                )
            else:
                h.ok(
                    {
                        "id": body["repository"].rsplit("/", 1)[-1],
                        "repository": {"full_name": body["repository"].split("github.com/")[-1]},
                        "origin": "linked",
                    },
                    status=201,
                )
        case ("PUT", ["projects", _, "branches", *branch, "stage"]):
            if body.get("stage") == "production" and not body.get("confirm_production"):
                raise Problem(400, "Making a branch production needs confirm_production.")
            h.ok({"name": "/".join(branch), "stage": body["stage"]})
        case ("POST", ["projects", _, "branches", *branch, "instances"]):
            name = body.get("name") or "-".join(branch)
            state.instances[name] = instance(name, status="creating")
            h.job(state.new_job(name, "Create instance"))
        case ("GET", ["jobs"]):
            h.ok(list(state.jobs.values()), {"total": len(state.jobs)})
        case ("GET", ["jobs", job_id]):
            if job_id not in state.jobs:
                raise Problem(404, "No such job.")
            h.ok(state.advance(state.jobs[job_id]))
        case ("GET", ["jobs", job_id, "log"]):
            h.ok({"text": "step one\nstep two\n"}, {"next_offset": 2})
        case ("GET", ["events"]):
            h.events()
        case ("GET", ["backups"]):
            h.ok(
                [
                    {
                        "file": "sales-main__sales_main__20261112-000012__daily.zip",
                        "instance": "sales-main",
                        "kind": "daily",
                        "size": 48213504,
                        "created": 1794441612.4,
                        "series": "19.0",
                    },
                    {
                        "file": "shop__shop__20261112-000012__manual.zip",
                        "instance": "shop",
                        "kind": "manual",
                        "size": 1000,
                        "created": 1794441612.4,
                        "series": "19.0",
                    },
                ]
            )
        case ("GET", ["backups", file]):
            if not file.endswith(".zip"):
                raise Problem(404, "No such backup.")
            h._send(200, raw=b"PK" + b"x" * 200_000, ctype="application/zip")
        case ("GET", ["offsite"]):
            h.ok(
                {"enabled": True, "configured": True, "active": True, "location": "s3://bucket", "fingerprint": "ab:cd"}
            )
        case ("GET", ["offsite", "copies"]):
            h.ok(
                [
                    {
                        "id": "c1",
                        "instance": "sales-main",
                        "kind": "daily",
                        "status": "stored",
                        "size": 1000,
                        "created": 1794441612,
                    }
                ]
            )
        case ("POST", ["offsite", "copies", copy_id, action]):
            if copy_id != "c1":
                raise Problem(404, "No such copy.")
            h.job(
                state.new_job(
                    body.get("instance", "sales-main"),
                    {"verify": "Verify off-site copy", "restore": "Restore off-site copy"}[action],
                )
            )
        case ("GET", ["users"]):
            h.ok(
                [
                    {
                        "name": "sam",
                        "display_name": "Sam Lee",
                        "email": "sam@example.com",
                        "role": "admin",
                        "role_label": "Administrator",
                        "second_factors": ["totp"],
                    }
                ]
            )
        case ("GET", ["roles"]):
            h.ok(
                {
                    "roles": [
                        {"name": "admin", "label": "Administrator", "description": "Everything", "permissions": []}
                    ],
                    "permissions": {},
                }
            )
        case _:
            raise Problem(404, f"No route {method} {path}.")


class MockPanel(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.state = default_state()
        self.thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def __enter__(self) -> MockPanel:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()
        self.server_close()
