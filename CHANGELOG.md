# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Unit tests for Shelly Modbus register encoding and FC03/FC04 PDU behavior.
- RPC fixture tests aligned to `tests/fixtures/real_shelly/` captures.
- Wire-server HTTP header tests (ShellyHTTP/1.0.0, no `Date`).
- `scripts/modbus_smoke.py` for local and CI Modbus TCP checks.
- Dependabot for pip, Docker base image, and GitHub Actions.
- README documentation for extended env vars, Modbus examples, and `compare_shelly.sh`.

### Changed

- Python **3.13** in Docker and CI (from 3.11).
- Pinned Python dependencies in `requirements.txt` for reproducible builds.
- Docker image runs as non-root `app` user; documents ports **80** and **502**.
- CI runs Modbus smoke test against the container’s Shelly Modbus server.

## [0.1.4] — prior releases

See [GitHub Releases](https://github.com/wgentine/sigelly_emu/releases) for earlier tags (`v0.1.0` … `v0.1.4`).
