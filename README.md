# sigelly_emu — Shelly Pro 3EM-3CT63 emulator (Alfen Modbus source)

Emulates a **Shelly Pro 3EM-3CT63** on the LAN so Sigenergy Sigenstor / mySigen can discover it and read live power. Measurement values are polled from an **Alfen Eve Pro Single** over Modbus TCP.

This is a deliberate v2 rewrite. The earlier minimal Flask prototype (`virtual_shelly.py`) was too incomplete for real Shelly/Sigenstor clients.

## Architecture

```
Alfen Eve Pro Single  --Modbus TCP:502-->  sigelly_emu  --HTTP RPC + mDNS-->  Sigenstor
     (slave ID 1)                         (Docker / host)
```

## Quick start (Docker)

1. Copy env and edit Alfen + network settings:

```bash
cp .env.example .env
# set ALFEN_HOST, optionally SHELLY_ADVERTISE_IP
```

2. Start with host networking (recommended for mDNS + port 80):

```bash
docker compose up -d --build
```

3. Validate the Shelly API surface before pairing Sigenstor:

```bash
./scripts/validate.sh http://127.0.0.1
# or: ./scripts/validate.sh http://<advertise-ip>
```

4. Open diagnostics while pairing:

```text
http://<advertise-ip>/debug
```

### Pre-built images (GitHub Releases)

Publishing a GitHub Release builds and pushes the image to GHCR:

```bash
docker pull ghcr.io/wgentine/sigelly_emu:latest
# or a specific tag, e.g. 0.1.0 / v0.1.0
docker pull ghcr.io/wgentine/sigelly_emu:0.1.0
```

Point `docker-compose.yml` at that image, or run:

```bash
docker run --rm --network host --env-file .env \
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
| `SHELLY_MODEL` | `SPEM-003CEBEU63` | Device model string |
| `SHELLY_FIRMWARE` | `1.4.4` | Reported firmware version |
| `SHELLY_APP` | `Pro3EM` | mDNS / device app id |
| `HTTP_PORT` | `80` | HTTP listen port |
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

## Local run (without Docker)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit ALFEN_HOST; for non-root use HTTP_PORT=8080
export STATE_PATH=./data/state.json
uvicorn app.main:app --host 0.0.0.0 --port 8080
./scripts/validate.sh http://127.0.0.1:8080
```

## Sigenstor / mySigen pairing

1. Run `./scripts/validate.sh` successfully first.
2. Put Sigen gateway and this host on the **same Wi‑Fi / LAN subnet**.
3. Disable Wi‑Fi client / AP isolation (breaks mDNS).
4. Do **not** enable Shelly auth (`auth_en` must stay false).
5. Phone on the same Wi‑Fi as Sigen during pairing.
6. mySigen → Add device → Smart Load → **WLAN Network**.
7. Watch `/debug` for which RPC methods Sigen calls.

If the WLAN scan shows unrelated “unknown” ESP32 devices, try a cleaner test SSID/VLAN with only Sigen + this emulator.

**Note:** Some users report mySigen showing ~35 W more than the Shelly reading. That is a Sigen quirk; this emulator does not invent offsets.

## Bridge networking alternative

If host networking is unavailable:

```yaml
# not recommended for mDNS — example only
services:
  sigelly-emu:
    build: .
    ports:
      - "8080:80"
    environment:
      SHELLY_ADVERTISE_IP: "192.168.x.x"  # host LAN IP
      HTTP_PORT: "80"
    env_file: .env
    volumes:
      - sigelly-data:/data
```

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
| Sigen never finds device | host network, mDNS, same subnet, AP isolation, `/debug` mDNS section |
| Found but won't enroll | `GET /shelly` must show `auth_en: false`; no password |
| Power always 0 | Alfen Modbus enabled? `ALFEN_HOST` reachable? `/debug` Alfen section |
| Nonsense power values | Byte order / Modbus map — compare Alfen UI vs `/rpc/EM.GetStatus` |
| Port 80 permission denied | Run via Docker, or set `HTTP_PORT=8080` locally |
