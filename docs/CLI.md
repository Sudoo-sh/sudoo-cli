# The sudoo command

**Audience:** developers and administrators who drive a Sudoo.sh panel from a terminal, a script or a CI pipeline.
**Covers:** installing `sudoo`, logging in, profiles, every command, output for scripts, exit codes, CI examples,
and what the command cannot do.

`sudoo` talks to your panel's API (`/api/v1`) with an API token, and signs every request the way the panel requires.
Everything it does, the panel's web app can do too; the panel checks the same permissions.

---

## Contents

1. [Installing](#1-installing)
2. [Logging in](#2-logging-in)
3. [Profiles and environment variables](#3-profiles-and-environment-variables)
4. [Commands](#4-commands)
5. [Jobs](#5-jobs)
6. [Output for scripts](#6-output-for-scripts)
7. [Exit codes](#7-exit-codes)
8. [CI examples](#8-ci-examples)
9. [Security](#9-security)
10. [What sudoo cannot do](#10-what-sudoo-cannot-do)

---

## 1. Installing

`sudoo` needs Python 3.12 or later, and nothing else. It is published on PyPI as
[`sudoo-cli`](https://pypi.org/project/sudoo-cli/).

| How | Command |
| :--- | :--- |
| pipx | `pipx install sudoo-cli` |
| uv | `uv tool install sudoo-cli` |
| pip, in a virtual environment | `pip install sudoo-cli` |
| With the OS keyring for credentials | `pipx install 'sudoo-cli[keyring]'` |
| One file, on a server without pip | Download `sudoo.pyz` from the [latest release](https://github.com/Sudoo-sh/sudoo-cli/releases/latest), check it against `SHA256SUMS`, then `chmod +x sudoo.pyz` and run `./sudoo.pyz` (or `python3 sudoo.pyz`) |

Check it works with `sudoo version`.

Shell completion:

```bash
eval "$(sudoo completion bash)"                          # in ~/.bashrc
eval "$(sudoo completion zsh)"                           # in ~/.zshrc
sudoo completion fish > ~/.config/fish/completions/sudoo.fish
```

---

## 2. Logging in

Create an API token in the panel first: **API tokens** in the sidebar, then **Create token**. Choose the scope:

| Scope | Use it for |
| :--- | :--- |
| `read` | Looking only: lists, status, logs, jobs. Every change is refused. |
| `instances` | Connected apps: list, create, start, pause, restart, back up and delete instances. It cannot deploy code, read logs, projects or backups, and cannot open the live event stream (`sudoo` then follows jobs by polling). |
| `write` | Everything an Administrator may do, for example `sudoo deploy` from CI. |

The panel shows the **token** (`sudoo_...`) and the **signing secret** once. Then:

```bash
sudoo login
# Panel address (https://...): https://panel.example.com
# API token (sudoo_...):          (hidden)
# Signing secret:                 (hidden)
```

`sudoo login` checks the credentials with the panel before keeping them. Options:

| Option | Meaning |
| :--- | :--- |
| `--url URL` | The panel's address, instead of the question |
| `--profile NAME` | Keep them under this profile name (default: `default`) |
| `--default` | Make this profile the default one |
| `--ca-cert FILE` | The panel uses a certificate from a private certificate authority: trust this CA (PEM) |
| `--acting-for PERSON` | Send `X-Sudoo-Acting-For` with every call (see [Panel API](https://docs.sudoo.sh/panel-api#acting-for-someone)) |
| `--no-verify` | Keep the credentials without checking them |

The token and the signing secret are kept in the OS keyring when `sudoo-cli[keyring]` is installed and a keyring
works on the machine. Otherwise they are kept in `~/.config/sudoo/credentials.json`, which only your user can read
(mode 0600, folder 0700). The panel's address and options are in `~/.config/sudoo/config.json`, never a secret.

`sudoo logout` forgets the profile and its credentials. To end a token everywhere, revoke it in the panel.

---

## 3. Profiles and environment variables

A profile is one panel and one token. Use several for several panels, or for a `read` and a `write` token:

```bash
sudoo login --profile prod --url https://panel.example.com --default
sudoo login --profile lab  --url https://lab.example.com
sudoo profiles                         # list them
sudoo --profile lab instances list     # use one for one command
export SUDOO_PROFILE=lab               # or for the whole shell
```

Environment variables override the profile, and need no file at all, which is what CI wants:

| Variable | Meaning |
| :--- | :--- |
| `SUDOO_URL` | The panel's address |
| `SUDOO_TOKEN` | The API token |
| `SUDOO_SECRET` (or `SUDOO_SIGNING_SECRET`) | The signing secret |
| `SUDOO_PROFILE` | The profile to use when `--profile` is not given |
| `SUDOO_CA_CERT` | A CA certificate (PEM) to trust |
| `SUDOO_ACTING_FOR` | The `X-Sudoo-Acting-For` value |
| `SUDOO_CONFIG_DIR` | Where to keep profiles (default `$XDG_CONFIG_HOME/sudoo`, else `~/.config/sudoo`) |
| `SUDOO_KEYRING=0` | Never use the OS keyring |

Global options work before or after the command: `sudoo --json instances list` is `sudoo instances list --json`.

| Option | Meaning |
| :--- | :--- |
| `--profile NAME`, `-p` | The profile |
| `--json` | Print JSON (see [Output for scripts](#6-output-for-scripts)) |
| `--quiet`, `-q` | No progress lines while following a job |
| `--debug` | Print each request and answer on stderr: method, path, status, time and request id, with the token and signature replaced by `<redacted>` |
| `--ca-cert FILE` | Trust this CA certificate for the panel's TLS |
| `--acting-for PERSON` | Who in your system the call is made for |
| `--timeout SECONDS` | For each request (default 60) |

---

## 4. Commands

`sudoo COMMAND --help` describes every option. The permission each call needs is in
[Panel API](https://docs.sudoo.sh/panel-api#3-endpoints).

### Panel

| Command | Does |
| :--- | :--- |
| `sudoo status` | The panel's health (`/healthz`), version, plugins, licence, running instances, instance limit, capacity, CPU, memory and disk |
| `sudoo version` | The version of `sudoo` |
| `sudoo login`, `logout`, `profiles` | [Logging in](#2-logging-in) |
| `sudoo completion bash\|zsh\|fish` | A shell completion script |

### Projects and branches

| Command | Does |
| :--- | :--- |
| `sudoo projects list` | Every project: repository, Odoo version, branches, instances |
| `sudoo projects show PROJECT [--refresh]` | A project and its branches (`--refresh` reads GitHub now) |
| `sudoo projects link https://github.com/acme/erp --odoo-version 19.0 [--edition enterprise] [--name NAME]` | A project for an existing repository |
| `sudoo projects create --owner acme --repo-name erp --odoo-version 19.0 [--public] [--description TEXT]` | A new, empty repository on GitHub and its project |
| `sudoo branches list PROJECT [--refresh]` | The branches, their stage, latest commit and instances |
| `sudoo branches set-stage PROJECT BRANCH STAGE [--confirm-production]` | Move a branch, and its instances, to `development`, `staging` or `production`; into or out of production needs `--confirm-production` |
| `sudoo branches create-instance PROJECT BRANCH [--name NAME] [--demo/--no-demo] [--no-install-modules]` | An instance of the branch in the branch's stage; staging starts from a neutralised copy of production |

A branch name may contain `/` (`feature/login`): type it as it is.

### Instances

| Command | Does |
| :--- | :--- |
| `sudoo instances list [--project P] [--status S] [--stage S]` | Every instance (all pages) |
| `sudoo instances show NAME` | Status, stage, address, code, settings, usage, lifetime, the running job |
| `sudoo instances create --odoo-version 19.0 [--repo URL --branch B] [--name N] [--stage S] [--edition E] [--delete-after-days N] [--demo/--no-demo] [--no-install-modules] [--from-backup FILE]` | A new instance; without `--repo`, plain Odoo |
| `sudoo instances start NAME` | Start a paused or stopped instance |
| `sudoo instances pause NAME [--confirm-production]` | Pause it; it starts again on its next visit |
| `sudoo instances restart NAME` | Restart Odoo |
| `sudoo instances stop NAME [--confirm-production]` | Switch it off; it stays off until started |
| `sudoo instances delete NAME [--yes] [--confirm-production] [--without-backup]` | Delete it after a backup. Asks you to type the name, unless `--yes` |
| `sudoo instances set NAME [--stage S] [--delete-after-days N] [--auto-update/--no-auto-update] [--auto-upgrade/...] [--auto-pause/...] [--confirm-production]` | Change its settings or stage |
| `sudoo instances logs NAME [-n LINES] [--follow] [--interval S]` | The end of Odoo's log; `--follow` keeps printing new lines (Ctrl+C ends it) |
| `sudoo instances open NAME [--print]` | Print its address and open it in your browser (`--print`: only print) |

### Deploying

`sudoo deploy` gets the latest code of the branch (the panel's **Get latest code**) and follows the job:

```bash
sudoo deploy sales-staging                          # one instance
sudoo deploy --project sales --branch main          # every instance of the branch
sudoo deploy sales-staging --submodules             # update the submodules instead
```

With safe updates on (the default, and always for production), the panel backs the instance up first and puts it
back by itself when the update fails; `sudoo` then ends with exit code 11 and the panel's explanation.

### Backups

| Command | Does |
| :--- | :--- |
| `sudoo backups list [--instance NAME]` | Backups on the server, newest first. Without `backups.manage`, `--instance` still lists that instance's own backups |
| `sudoo backups list --offsite [--instance NAME]` | The off-site copies and their state (`stored`, `verified`, ...) |
| `sudoo backups create NAME` | Back an instance up now |
| `sudoo backups restore NAME [--file FILE] [--yes] [--confirm-production]` | Restore a backup (the latest without `--file`); a `before-restore` backup is taken first |
| `sudoo backups restore NAME --offsite COPY_ID [--public-key PEM]` | Download an off-site copy, check it (fails closed) and restore it |
| `sudoo backups download FILE [-o PATH] [--force]` | Save a backup's zip; written to a temporary file and renamed only when complete |
| `sudoo backups verify COPY_ID [--public-key PEM]` | Download and check an off-site copy, with a key kept off the server if you give one |
| `sudoo backups offsite` | The off-site settings: location, stages, kinds, key fingerprint, last scan |

### Domains

| Command | Does |
| :--- | :--- |
| `sudoo domains list NAME` | The mode, where to point DNS, and every custom domain with its state |
| `sudoo domains add NAME DOMAIN [--confirm-production]` | Add a domain; prints the DNS records to create |
| `sudoo domains status NAME DOMAIN [--check]` | One domain's state; `--check` checks DNS (and issues the certificate) now |
| `sudoo domains primary NAME DOMAIN [--confirm-production]` | Make it the primary address |
| `sudoo domains remove NAME DOMAIN [--yes] [--confirm-production]` | Remove it; its certificate is deleted |

### Caught mail, resources, jobs and accounts

| Command | Does |
| :--- | :--- |
| `sudoo mail list NAME [--search WORDS] [--limit N] [--offset N]` | E-mails the mail catcher caught from a staging or development instance |
| `sudoo mail show NAME ID` | One e-mail: addresses, subject, attachments and text |
| `sudoo resources show NAME` | Workers and limits: what applies, the instance's own value, the stage's default |
| `sudoo resources set NAME KEY=VALUE ... [--confirm-production]` | Set limits, for example `odoo_workers=4 odoo_memory_limit=4g`; `KEY=default` follows the stage again. The containers are recreated (a job) |
| `sudoo jobs list [--instance NAME] [--limit N]` | Recent jobs, newest first |
| `sudoo jobs show ID [--log]` | One job, its steps, notes and error (`--log`: its technical log) |
| `sudoo jobs follow ID` | Follow a job until it ends |
| `sudoo jobs log ID [--offset N]` | A job's technical log |
| `sudoo users list` | The panel's accounts (a `write` token) |
| `sudoo users roles` | The roles and what each allows |

### Teams

Teams group panel accounts so that a project or a branch can be given to them at once. Teams are a **premium
feature of the Pro and Business plans**: on another plan the panel answers 403 (exit 4) when you create a team, add
members or give access, while listing, taking members out, taking access away and deleting always work. Reading
teams needs `teams.manage` or `projects.access`; changing them needs `teams.manage`, and grants need
`projects.access` in the project. A team is named by its id or its name (case aside).

| Command | Does |
| :--- | :--- |
| `sudoo teams list` | Every team, its number of members and the projects that name it |
| `sudoo teams show TEAM` | One team: description, members with their roles, projects |
| `sudoo teams create NAME [--description TEXT] [--member ACCOUNT ...]` | Make a team (409, exit 6, when the name is taken) |
| `sudoo teams rename TEAM NAME [--description TEXT]` | A new name (and description) |
| `sudoo teams delete TEAM [--yes]` | Delete it; it leaves every grant and branch lock |
| `sudoo teams members add TEAM ACCOUNT ...` | Add panel accounts to the team |
| `sudoo teams members remove TEAM ACCOUNT ...` | Take accounts out (exit 5 for one that is not in it) |
| `sudoo teams grants list PROJECT` | The project's access mode and its grants (teams and accounts), the branches' too |
| `sudoo teams grants set TEAM PROJECT LEVEL [--branch BRANCH]` | Give the team `none`, `view`, `developer`, `maintainer` or `admin` in the project, or only in one branch; it replaces the team's earlier level there |
| `sudoo teams grants remove TEAM PROJECT [--branch BRANCH]` | Take the team's grant away; the panel roles (or its other grants) decide again |

A grant never gives more than the account's role allows. `grants set` and `grants remove` read the project's access
and send it back whole with the one change (`PUT /projects/<id>/access`); the panel answers 409 when the change
would leave you without `projects.access` in the project.

---

## 5. Jobs

Creating, starting, deploying, restoring and the like run as **jobs** on the panel. `sudoo` waits for the job and
prints a line on stderr whenever its step or progress changes:

```
Get latest code on sales-staging (job a12fff66a05d)
  [  0%] Get latest code (queued)
  [ 50%] Upgrading modules (running)
  [100%] Get latest code (success)
Get latest code finished in 48 s.
```

It follows the job through the panel's live event stream and, when that is not available (an `instances` token, a
proxy that cuts long connections), by reading the job every 2 seconds. Options of every command that starts a job:

| Option | Meaning |
| :--- | :--- |
| `--no-wait` | Return once the panel accepted the job; prints `sudoo jobs follow <id>` |
| `--poll` | Follow by polling instead of the event stream |
| `--wait-timeout SECONDS` | Stop waiting after this long (exit code 1); the job goes on |

When the panel answers **429 Too Many Requests**, `sudoo` waits the `Retry-After` seconds and tries again, three
times, with a new signature each time; then it ends with exit code 8.

---

## 6. Output for scripts

Without `--json`, `sudoo` prints tables and lists for people, and notes and progress on **stderr**, so stdout stays
clean. With `--json`:

* list and show commands print the panel's answer (`{"data": ..., "meta": ...}`), or the list itself for
  `instances list` and `backups list`;
* commands that start a job print the finished job (with `--no-wait`, the accepted one); a deploy to several
  instances prints one job per instance, one after the other;
* an error prints `{"error": {"message", "exit_code", "status", "problem"}}`, `problem` being the panel's
  problem detail, with its `request_id`.

```bash
url=$(sudoo instances show pr-42 --json | jq -r .data.url)
sudoo instances list --json | jq -r '.[] | select(.status == "paused") | .name'
```

---

## 7. Exit codes

| Code | Meaning |
| :--- | :--- |
| 0 | Done |
| 1 | Another error (also `--wait-timeout` reached) |
| 2 | A wrong command line, or a confirmation refused or missing (`--yes` not given outside a terminal) |
| 3 | Not logged in, or the panel refused the token or the signature (401) |
| 4 | The token's scope or role does not allow it (403); also `sudoo shell` |
| 5 | Not found (404): no such instance, project, job, backup or domain |
| 6 | Conflict (409): for example the instance is still being created |
| 7 | The panel refused a value (400, 415) |
| 8 | Still too many requests (429) after waiting |
| 9 | The panel failed (5xx) |
| 10 | The panel could not be reached: name, connection, TLS certificate or timeout |
| 11 | A followed job failed |
| 130 | Interrupted (Ctrl+C) |

---

## 8. CI examples

Keep the token and the signing secret as CI secrets, never in the repository.

**Deploy on every push to main** (GitHub Actions; a `write` token):

```yaml
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - name: Deploy to the panel
        env:
          SUDOO_URL: ${{ vars.SUDOO_URL }}
          SUDOO_TOKEN: ${{ secrets.SUDOO_TOKEN }}
          SUDOO_SECRET: ${{ secrets.SUDOO_SECRET }}
        run: |
          pipx install sudoo-cli==3.0.0
          sudoo deploy --project sales --branch main
```

**An instance for every pull request**, deleted after 7 days:

```yaml
      - name: Preview instance
        env:
          SUDOO_URL: ${{ vars.SUDOO_URL }}
          SUDOO_TOKEN: ${{ secrets.SUDOO_TOKEN }}
          SUDOO_SECRET: ${{ secrets.SUDOO_SECRET }}
        run: |
          pipx install sudoo-cli==3.0.0
          name="pr-${{ github.event.number }}"
          if sudoo instances show "$name" --quiet >/dev/null 2>&1; then
            sudoo deploy "$name"
          else
            sudoo instances create --repo "https://github.com/${{ github.repository }}" \
              --branch "${{ github.head_ref }}" --odoo-version 19.0 --name "$name" --delete-after-days 7
          fi
          echo "url=$(sudoo instances show "$name" --json | jq -r .data.url)" >> "$GITHUB_OUTPUT"
```

**GitLab CI** or any shell: the same commands, with the three variables set as masked CI variables. On a server
without pip, use the zipapp: `python3 sudoo.pyz deploy sales-staging`.

---

## 9. Security

* Every request is signed: an HMAC-SHA256 over a timestamp, a new random nonce, the method, the path and the body's
  SHA-256 (and `X-Sudoo-Acting-For` when sent), exactly as [Panel API](https://docs.sudoo.sh/panel-api#2-signing-a-request)
  describes. A copied token alone is useless without the signing secret.
* TLS certificates are always verified. A panel with a private certificate authority works with `--ca-cert FILE`
  (or `SUDOO_CA_CERT`); there is no option to switch verification off. Plain `http://` is accepted only for
  `localhost` and loopback addresses.
* `sudoo` never follows a redirect: the signature covers the path, and the token must not go to another host.
* `sudoo` never prints the token or the signing secret; `--debug` replaces them with `<redacted>`.
* Give each pipeline its own token with the smallest scope that works, and revoke tokens no longer used.

---

## 10. What sudoo cannot do

| Wish | Why not, and what to do |
| :--- | :--- |
| `sudoo shell` | The panel gives shell tickets only to people signed in to its web app, never to an API token, whatever its scope. `sudoo shell NAME` prints the address of the instance's **Shell** tab and ends with exit code 4. |
| Cancel a job | The panel's API has no call to cancel a job. Jobs end by themselves; safe updates put an instance back on failure. |
| Teams | The panel has accounts and roles, no teams: `sudoo users list` and `sudoo users roles`. |
| Settings, plugins, tokens, GitHub App | Use the panel's web app (most need a super admin's session). |
