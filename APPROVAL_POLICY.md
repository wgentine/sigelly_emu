# sigelly_emu — default approval policy

Repository-wide rules for Cursor PR Routing & Approval evaluating pull requests in this repository.

## Purpose

sigelly_emu emulates Shelly Pro 3EM hardware for Sigenergy pairing and reads live measurements from Alfen Modbus. Incorrect register mapping, Docker/network defaults, or dependency changes can break production pairing on customer LANs. Default posture: **approve only when risk is low and automated review is clean**.

## Auto-approve when ALL are true

- Change is limited in scope (typically tests-only, documentation-only, or a small bug fix with matching test coverage)
- Required CI checks for the changed paths are green
- Bugbot Review Context reports no findings that require human review
- Security Review Context reports no findings that require human review (when enabled for the team)
- Risk score is at or below the automation’s configured maximum threshold (when risk scoring is enabled)
- No paths listed under **Never auto-approve** are modified (unless a more specific subtree policy explicitly allows it)
- The PR does not modify approval policy, routing policy, or workflow guardrails in the same PR (base-branch policy applies per Cursor policy precedence)

## Safe auto-approve examples

- Documentation-only edits (`README.md`, comments, markdown under `docs/` if added)
- Test-only changes under `tests/**` that do not alter production defaults or Docker entrypoints
- Small application bug fixes with tests that do not touch Modbus maps, Alfen client behavior, or network/mDNS defaults

## Never auto-approve

- Any change under `.github/**` (workflows, Dependabot config, issue templates, branch rules encoded in-repo)
- `Dockerfile`, `docker-compose.yml`, `docker-compose.*.yml`, or container entrypoint scripts that affect production images
- Release or versioning artifacts: `CHANGELOG.md`, `.github/workflows/release-docker.yml`, release tags, or version constants shipped in images
- `APPROVAL_POLICY.md`, `.cursor/approval-policies/**`, or other approval/routing policy files
- `SECURITY.md` or security disclosure process changes
- Dependency changes that bump a **major** version (e.g. `requirements.txt`, GitHub Actions majors, base image majors) — request **@wgentine**
- Changes that remove or weaken tests, skip CI jobs, or broaden workflow permissions
- PRs labeled `security`, `breaking`, or `do-not-auto-approve`

## Reviewer routing

When auto-approval is not allowed, request **@wgentine** for review. Leave the PR unapproved with a short comment summarizing why human review is required.

## Conflict resolution

If this file conflicts with a closer `APPROVAL_POLICY.md` or a routed policy in `.cursor/approval-policies/`, follow the **more specific** policy. If specificity is unclear, follow the **stricter** rule and do not auto-approve.