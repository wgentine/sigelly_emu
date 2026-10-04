# sigelly_emu — Shelly Pro 3EM-3CT63 emulator (Alfen Modbus source)

Emulates a **Shelly Pro 3EM-3CT63** on the LAN so Sigenergy Sigenstor / mySigen can discover it and read live power. Measurement values are polled from an **Alfen Eve Pro Single** over Modbus TCP.

## Architecture

```
Alfen Eve Pro Single --Modbus TCP:502--> sigelly_emu --Shelly Modbus TCP:502--> Sigenstor
     (slave ID 1)                        (Docker)    --HTTP :80 + mDNS------> discovery
```

Sigenstor’s live meter path is **Shelly Modbus TCP** (input registers, any unit id — mySigen uses 7). HTTP RPC and mDNS are used for discovery / identity; the production entrypoint is `python -m app.wire_server` (byte-accurate ShellyHTTP headers).

## Quick start (Docker)

1. Copy env and edit Alfen + network settings:

```bash
cp .env.example .env
# set ALFEN_HOST, optionally SHELLY_ADVERTISE_IP
```

2. Start with host networking (recommended for mDNS + port 80). Uses the pre-built GHCR image by default (`latest`, or set `SIGELLY_TAG`):

```bash
docker compose pull
docker compose up -d
```

To build from this tree instead, edit `docker-compose.yml` (swap `image:` for `build: .`) or run `docker compose up -d --build` after enabling `build:`.

Publish **both** TCP **80** and **502** to the LAN (host/macvlan networking). Port 502 is required for Sigenstor metering.

3. Validate the Shelly HTTP API surface before pairing Sigenstor:

```bash
./scripts/validate.sh http://127.0.0.1
# or: ./scripts/validate.sh http://<advertise-ip>
```

4. Confirm Modbus (optional, same path Sigen uses after enroll):

```bash
# Phase A voltage @ input register 1020 (Shelly doc addr 31020); unit id 7 like mySigen
python3 scripts/modbus_smoke.py --host 127.0.0.1 --port 502 --unit 7

# Or with mbpoll (if installed):
# mbpoll -a 7 -r 1020 -c 2 -t 3:int -1 127.0.0.1 -p 502
```

5. Open diagnostics while pairing:

```text
http://<advertise-ip>/debug
```

### Pre-built images (GitHub Releases)

Publishing a GitHub Release builds and pushes the image to GHCR. `docker-compose.yml` pulls that image by default:

```bash
docker pull ghcr.io/wgentine/sigelly_emu:latest
# or pin a release, e.g.:
SIGELLY_TAG=0.1.0 docker compose up -d
```

Or run without Compose:

```bash
docker run --rm --network host --cap-add=NET_BIND_SERVICE --env-file .env \
  -v sigelly-data:/data \
  ghcr.io/wgentine/sigelly_emu:latest
```

## Alfen prerequisites

In **ACE Service Installer**:

- Enable Modbus TCP (wired Ethernet, port **502**)
- Active Load Balancing enabled
- Data source: **Energy Management System** (station acts as Modbus slave)
- Socket measurements use **slave/unit ID 1** on Eve Single

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ALFEN_HOST` | *(required)* | Alfen IP / hostname |
| `ALFEN_PORT` | `502` | Modbus TCP port |
| `ALFEN_SLAVE_ID` | `1` | Socket unit ID |
| `ALFEN_POLL_INTERVAL` | `2.0` | Seconds between polls |
| `ALFEN_CONNECT_TIMEOUT` | `3.0` | Modbus connect/read timeout |
| `SHELLY_DEVICE_ID` | `349454112233` | 12-char hex ID used in mDNS name |
| `SHELLY_MAC` | `34:94:54:11:22:33` | Reported MAC |
| `SHELLY_MODEL` | `SPEM-003CEBEU63` | Device model string (real 3CT63 SKU) |
| `SHELLY_FIRMWARE` | `2.0.0` | Reported firmware version |
| `SHELLY_FW_ID` | `20260710-101221/2.0.0-g87fbfa4` | Full firmware build id (`fw_id` in RPC) |
| `SHELLY_SN` | `EMU000001` | Serial string in `Sys.GetStatus` |
| `SHELLY_APP` | `Pro3EM` | mDNS / device app id |
| `SHELLY_WIFI_SSID` | *(empty)* | WiFi SSID if emulating WiFi client; empty = eth-only |
| `SHELLY_TZ` | `Europe/Amsterdam` | Timezone for `Sys.GetStatus` |
| `SHELLY_LAT` / `SHELLY_LON` | `52.3346` / `4.8914` | Location in `Sys.GetConfig` |
| `ALFEN_USE_ENERGY` | `false` | `true` = Alfen lifetime Wh; `false` = integrate from power |
| `HTTP_PORT` | `80` | HTTP listen port |
| `SHELLY_MODBUS_ENABLE` | `true` | Expose Shelly Modbus TCP for Sigenstor |
| `SHELLY_MODBUS_PORT` | `502` | Shelly Modbus listen port |
| `SHELLY_MODBUS_UNIT_ID` | `1` | Preferred unit id (any id accepted; Sigen uses 7) |
| `SHELLY_ADVERTISE_IP` | *(auto)* | IP advertised via mDNS / wifi status |
| `MDNS_ENABLE` | `true` | Advertise `_http._tcp` and `_shelly._tcp` |
| `STATE_PATH` | `/data/state.json` | Persisted energy counters |
| `LOG_LEVEL` | `INFO` | Logging level |

## Exposed endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /shelly` | Gen2 device info (`auth_en: false`) |
| `POST /rpc` | JSON-RPC envelope (`id` / `src` / `result`) |
| `GET /rpc/{Method}?id=0` | Bare method result |
| `GET /rpc?method=...` | Alternate GET form |
| `GET /healthz` | Health / Alfen poll status |
| `GET /debug` | Pairing diagnostics (HTML or JSON) |

### Supported RPC methods

`Shelly.GetDeviceInfo`, `Shelly.GetStatus`, `Shelly.GetConfig`, `Shelly.ListMethods`, `EM.GetStatus`, `EM.GetConfig`, `EMData.GetStatus`, `EMData.GetConfig`, `Wifi.GetStatus`, `Sys.GetStatus`

## Compare against a real Shelly (optional)

To diff HTTP/RPC JSON against a physical Pro 3EM-3CT63 on your LAN:

```bash
./scripts/compare_shelly.sh http://<real-shelly-ip> http://127.0.0.1:8080
# Report under /tmp/shelly_compare/report.txt
```

Re-run after Shelly firmware or mySigen updates if pairing behavior changes; adjust `SHELLY_*` identity env vars only when captures diverge.

## Local run (without Docker)

Requires **Python 3.13+** (matches the Docker image).

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit ALFEN_HOST; for non-root use HTTP_PORT=8080 (and SHELLY_MODBUS_PORT if needed)
export STATE_PATH=./data/state.json
python -m app.wire_server
./scripts/validate.sh http://127.0.0.1:8080
```

`uvicorn app.main:app` remains available for HTTP-only debugging; production uses `app.wire_server`.

The published Docker image runs as a non-root `app` user. Binding ports **80** and **502** still requires `NET_BIND_SERVICE` (`cap_add` in Compose, or `--cap-add=NET_BIND_SERVICE` for `docker run`; included by default in Compose).

## Sigenstor / mySigen pairing

1. Run `./scripts/validate.sh` successfully first.
2. Ensure TCP **80** and **502** are reachable from the Sigen gateway on the same LAN/VLAN.
3. Disable Wi‑Fi client / AP isolation (breaks mDNS).
4. Do **not** enable Shelly auth (`auth_en` must stay false).
5. Phone on the same Wi‑Fi as Sigen during pairing.
6. mySigen → Add device → energy meter / Shelly path → **WLAN Network**.
7. After enroll, Sigen polls **Modbus TCP :502** (not HTTP) for live power/energy.

If the WLAN scan shows unrelated “unknown” ESP32 devices, try a cleaner test SSID/VLAN with only Sigen + this emulator.

**Note:** Some users report mySigen showing ~35 W more than the Shelly reading. That is a Sigen quirk; this emulator does not invent offsets.

## Bridge networking alternative

If host networking is unavailable (e.g. Docker Desktop), map host port **80** explicitly. mDNS discovery is often unreliable in this mode — set `SHELLY_ADVERTISE_IP` to the host LAN IP and prefer a Linux host with `network_mode: host` for Sigenstor pairing.

```yaml
# not recommended for mDNS — example only
services:
  sigelly-emu:
    image: ghcr.io/wgentine/sigelly_emu:${SIGELLY_TAG:-latest}
    ports:
      - "80:80"
      - "502:502"
    cap_add:
      - NET_BIND_SERVICE
    environment:
      SHELLY_ADVERTISE_IP: "192.168.x.x"  # host LAN IP
      HTTP_PORT: "80"
    env_file: .env
    volumes:
      - sigelly-data:/data
```

Binding host port 80 requires a privileged Docker daemon (not rootless). If that fails, use `8080:8080` with `HTTP_PORT=8080` (Sigenstor/mySigen typically expect port 80).

mDNS may still fail across Docker bridge networks; prefer `network_mode: host`.

## Register mapping (Alfen → Shelly)

Socket holding registers (slave 1), batch-read `306..409`:

- Voltage L1–L3 → `a/b/c_voltage`
- Current L1–L3 → `a/b/c_current`
- PF / frequency / real & apparent power → `em:0` fields
- Delivered / consumed energy (FLOAT64 Wh) → `emdata:0`

If Alfen energy registers are unavailable, power is integrated into Wh counters and persisted under `STATE_PATH`.

## Troubleshooting

| Symptom | Check |
|---------|--------|
| Sigen never finds device | host/macvlan network, mDNS, same subnet, AP isolation, `/debug` mDNS section |
| Found but won't enroll | `GET /shelly` must show `auth_en: false`; no password; TCP 502 open |
| Enrolls but no live data | Sigen must reach `:502`; watch for Modbus from the gateway IP |
| Power always 0 | Alfen Modbus enabled? `ALFEN_HOST` reachable? `/debug` Alfen section |
| Nonsense power values | Byte order / Modbus map — compare Alfen UI vs `/rpc/EM.GetStatus` |
| Port 80/502 permission denied | Need `cap_add: [NET_BIND_SERVICE]` (compose default). Rootless Docker cannot bind privileged ports |

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for release notes. Python dependencies are pinned in `requirements.txt`; Dependabot opens weekly update PRs.

## License

Copyright © 2026 W. Gentine

Licensed under the [GNU Affero General Public License v3.0](LICENSE).

You may use, modify, and distribute this software under the terms of AGPL-3.0.
If you modify this software and make it available over a network, you must
provide source code to users of that service.
