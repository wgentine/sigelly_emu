# Security Policy

## Supported versions

Security fixes are provided only for the **latest stable [GitHub release](https://github.com/wgentine/sigelly_emu/releases)** and the matching `latest` image on GHCR (`ghcr.io/wgentine/sigelly_emu:latest`). Pre-releases and older tagged releases are not supported.

## Reporting a vulnerability

**Do not** open a public GitHub issue for security problems.

Use [GitHub private vulnerability reporting](https://github.com/wgentine/sigelly_emu/security/advisories/new) (repository **Security** tab → **Report a vulnerability**).

Include, if you can:

- A clear description of the issue and likely impact
- Steps to reproduce, or a proof of concept
- Affected version or Docker image tag
- Any suggested fix or mitigation

This is a hobby project maintained by one person. There is no SLA. I aim to **acknowledge** reports within about **7 days** and will handle them as time allows.

## Scope

sigelly_emu is intended to run on a **trusted local network** (for example, home LAN wiring between an Alfen Eve Pro, this emulator, and a Sigenergy gateway).

The Shelly-compatible **HTTP** and **JSON-RPC** endpoints and the **Modbus TCP** server are **unauthenticated by design** (the emulator advertises `auth_en: false`, matching how it is used with Sigenstor). **Do not expose these ports to the internet.** Misconfiguration that leaves Modbus or HTTP reachable from untrusted networks is out of scope—not treated as a product vulnerability here.

Reports about missing authentication on those protocols will be closed as out of scope unless they show a practical impact **within** the intended trusted-LAN deployment model.