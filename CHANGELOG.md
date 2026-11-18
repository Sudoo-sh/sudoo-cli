# Changelog

All notable changes to `sudoo-cli` are listed here. The format follows [Keep a Changelog](https://keepachangelog.com),
and the project uses [semantic versioning](https://semver.org). Its version follows the panel's major version.

## Unreleased

## 3.0.0 - 2026-11-18

First release, for Sudoo.sh panels 3.x (`/api/v1`).

### Added

- Signed requests (API token and HMAC-SHA256 signing secret, optional `X-Sudoo-Acting-For`) exactly as the panel's
  API describes, with TLS verification always on and `--ca-cert` for a private certificate authority.
- `sudoo login`, `logout` and `profiles`: several panels, credentials in the OS keyring (optional extra `keyring`)
  or in a 0600 file; `SUDOO_URL`, `SUDOO_TOKEN` and `SUDOO_SECRET` for CI.
- Commands: `status`, `version`, `projects` (list, show, create, link), `branches` (list, set-stage,
  create-instance), `instances` (list, show, create, start, pause, restart, stop, delete, set, logs with `--follow`,
  open), `deploy`, `backups` (list, create, restore, download, verify, offsite; off-site copies included),
  `domains` (list, add, remove, status, primary), `mail` (list, show), `resources` (show, set), `jobs` (list, show,
  follow, log), `users` (list, roles), `completion` (bash, zsh, fish).
- Jobs are followed live through the event stream, or by polling when it is not available; 429 answers are retried
  after `Retry-After`.
- `--json` output and documented exit codes for scripts.
- A single-file zipapp (`sudoo.pyz`) attached to each GitHub release.
