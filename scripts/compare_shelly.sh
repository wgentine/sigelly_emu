#!/usr/bin/env bash
# Compare Shelly Gen2 HTTP/RPC payloads: real device vs sigelly_emu.
# Usage:
#   ./scripts/compare_shelly.sh http://192.168.30.35 http://127.0.0.1:8080
set -euo pipefail
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

REAL="${1:-http://192.168.30.35}"
EMU="${2:-http://127.0.0.1:8080}"
OUT="${3:-/tmp/shelly_compare}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

mkdir -p "$OUT"
rm -f "$OUT"/*.json "$OUT"/report.txt

PATHS=(
  /shelly
  /rpc/Shelly.GetDeviceInfo
  /rpc/Shelly.GetStatus
  /rpc/Shelly.GetConfig
  /rpc/Shelly.ListMethods
  /rpc/EM.GetStatus?id=0
  /rpc/EM.GetConfig?id=0
  /rpc/EMData.GetStatus?id=0
  /rpc/EMData.GetConfig?id=0
  /rpc/Sys.GetStatus
  /rpc/Wifi.GetStatus
  /rpc/Eth.GetStatus
  /rpc/Cloud.GetStatus
  /rpc/MQTT.GetStatus
  /rpc/Ble.GetStatus
  /rpc/Ws.GetStatus
  /rpc/Modbus.GetStatus
  /rpc/Temperature.GetStatus?id=0
)

safe_name() {
  echo "$1" | tr '/?&=:' '_____'
}

echo "REAL=$REAL"
echo "EMU=$EMU"
echo "OUT=$OUT"

for path in "${PATHS[@]}"; do
  name="$(safe_name "$path")"
  code_real="$(curl -sS -o "$OUT/real_${name}.json" -w '%{http_code}' --max-time 5 "${REAL}${path}" || true)"
  code_emu="$(curl -sS -o "$OUT/emu_${name}.json" -w '%{http_code}' --max-time 5 "${EMU}${path}" || true)"
  printf '%s  real=%s emu=%s\n' "$path" "$code_real" "$code_emu"
done

"$ROOT/.venv/bin/python" - "$OUT" <<'PY'
import json
import sys
from pathlib import Path

out = Path(sys.argv[1])


def load(path: Path):
    try:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return None, "empty"
        return json.loads(text), None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def keys(obj, prefix=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else k
            yield p
            yield from keys(v, p)
    elif isinstance(obj, list) and obj and not isinstance(obj[0], (dict, list)):
        return
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:1]):
            yield from keys(v, f"{prefix}[]")


def type_name(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int) and not isinstance(v, bool):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "dict"
    return type(v).__name__


def walk_types(obj, prefix=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else k
            yield p, type_name(v)
            yield from walk_types(v, p)
    elif isinstance(obj, list) and obj and isinstance(obj[0], (dict, list)):
        yield from walk_types(obj[0], f"{prefix}[]")


report = []
pairs = sorted({p.name[5:] for p in out.glob("real_*.json")})
for name in pairs:
    real_p = out / f"real_{name}"
    emu_p = out / f"emu_{name}"
    real, real_err = load(real_p)
    emu, emu_err = load(emu_p)
    report.append(f"\n=== {name} ===")
    if real_err:
        report.append(f"REAL load error: {real_err} body={real_p.read_text()[:120]!r}")
        continue
    if emu_err:
        report.append(f"EMU load error: {emu_err} body={emu_p.read_text()[:120]!r}")
        # still show real top-level keys as target
        if isinstance(real, dict):
            report.append("REAL top-level keys: " + ", ".join(sorted(real.keys())))
        continue

    if not isinstance(real, dict) or not isinstance(emu, dict):
        report.append(f"types real={type_name(real)} emu={type_name(emu)}")
        continue

    rk = set(keys(real))
    ek = set(keys(emu))
    missing = sorted(rk - ek)
    extra = sorted(ek - rk)
    report.append(f"keys real={len(rk)} emu={len(ek)} missing_in_emu={len(missing)} extra_in_emu={len(extra)}")
    if missing:
        report.append("  missing in emu:")
        for k in missing[:80]:
            report.append(f"    - {k}")
        if len(missing) > 80:
            report.append(f"    ... +{len(missing)-80} more")
    if extra:
        report.append("  extra in emu:")
        for k in extra[:40]:
            report.append(f"    - {k}")
        if len(extra) > 40:
            report.append(f"    ... +{len(extra)-40} more")

    rt = dict(walk_types(real))
    et = dict(walk_types(emu))
    type_mismatches = []
    for k, t in rt.items():
        if k in et and et[k] != t and not (
            {et[k], t} <= {"int", "float"}  # numeric widen OK for meters
        ):
            type_mismatches.append((k, t, et[k]))
    if type_mismatches:
        report.append("  type mismatches:")
        for k, t, et_ in type_mismatches[:40]:
            report.append(f"    - {k}: real={t} emu={et_}")

text = "\n".join(report) + "\n"
(out / "report.txt").write_text(text, encoding="utf-8")
print(text)
print(f"Wrote {out / 'report.txt'}")
PY
