#!/usr/bin/env bash
# Compatibility checks for Shelly Gen2 Pro 3EM-3CT63 emulator surface.
set -euo pipefail

BASE_URL="${1:-http://127.0.0.1:80}"
BASE_URL="${BASE_URL%/}"
FAIL=0

red() { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
info() { printf '==> %s\n' "$*"; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || { red "missing dependency: $1"; exit 1; }
}

need_cmd curl
need_cmd python3

check_json() {
  local name="$1"
  local url="$2"
  local py="$3"
  info "$name"
  local body
  if ! body="$(curl -fsS --max-time 5 "$url")"; then
    red "FAIL $name: curl error for $url"
    FAIL=1
    return
  fi
  if ! printf '%s' "$body" | python3 -c "$py"; then
    red "FAIL $name"
    printf '%s\n' "$body" | head -c 500
    echo
    FAIL=1
  else
    green "OK   $name"
  fi
}

check_post() {
  local name="$1"
  local payload="$2"
  local py="$3"
  info "$name"
  local body
  if ! body="$(curl -fsS --max-time 5 -H 'Content-Type: application/json' \
      -d "$payload" "$BASE_URL/rpc")"; then
    red "FAIL $name: curl error"
    FAIL=1
    return
  fi
  if ! printf '%s' "$body" | python3 -c "$py"; then
    red "FAIL $name"
    printf '%s\n' "$body" | head -c 800
    echo
    FAIL=1
  else
    green "OK   $name"
  fi
}

check_json "GET /shelly" "$BASE_URL/shelly" '
import json,sys
d=json.load(sys.stdin)
assert d.get("gen")==2, d
assert d.get("auth_en") is False, d
# Real Pro 3EM-3CT63 SKU (not the legacy marketing string SHPRO-3EM-3CT63)
model=str(d.get("model") or "")
assert model=="SPEM-003CEBEU63", d
assert d.get("app")=="Pro3EM", d
assert "mac" in d and d["mac"], d
assert d.get("id","").startswith("shellypro3em63-"), d
'

check_json "GET /rpc/EM.GetStatus?id=0" "$BASE_URL/rpc/EM.GetStatus?id=0" '
import json,sys
d=json.load(sys.stdin)
for k in ("a_act_power","b_act_power","c_act_power","total_act_power",
          "a_voltage","b_voltage","c_voltage","a_current","b_current","c_current",
          "a_pf","a_freq","a_aprt_power","total_aprt_power","total_current"):
    assert k in d, (k,d)
    assert isinstance(d[k], (int,float)), (k,d[k])
'

check_json "GET /rpc/EMData.GetStatus?id=0" "$BASE_URL/rpc/EMData.GetStatus?id=0" '
import json,sys
d=json.load(sys.stdin)
for k in ("a_total_act_energy","b_total_act_energy","c_total_act_energy",
          "total_act","a_total_act_ret_energy","total_act_ret"):
    assert k in d, (k,d)
'

check_json "GET /rpc?method=Shelly.GetDeviceInfo" \
  "$BASE_URL/rpc?method=Shelly.GetDeviceInfo" '
import json,sys
d=json.load(sys.stdin)
assert d.get("gen")==2, d
assert d.get("auth_en") is False, d
assert d.get("model"), d
'

check_post "POST /rpc Shelly.GetDeviceInfo envelope" \
  '{"id":1,"src":"validate","method":"Shelly.GetDeviceInfo"}' '
import json,sys
d=json.load(sys.stdin)
assert "result" in d, d
assert d["result"].get("auth_en") is False, d
assert "src" in d, d
'

check_post "POST /rpc Shelly.GetStatus has em:0 and emdata:0" \
  '{"id":2,"src":"validate","method":"Shelly.GetStatus"}' '
import json,sys
d=json.load(sys.stdin)
r=d["result"]
assert "em:0" in r, r.keys()
assert "emdata:0" in r, r.keys()
assert "sys" in r and "wifi" in r and "temperature:0" in r, r.keys()
em=r["em:0"]
assert "total_act_power" in em and "a_act_power" in em, em
'

check_post "POST /rpc unknown method returns 404 error envelope" \
  '{"id":3,"method":"Does.NotExist"}' '
import json,sys
d=json.load(sys.stdin)
assert "error" in d, d
assert d["error"].get("code")==404, d
'

check_json "GET /healthz" "$BASE_URL/healthz" '
import json,sys
d=json.load(sys.stdin)
assert "ok" in d, d
'

check_response_headers() {
  info "GET /rpc/WiFi.GetStatus response headers"
  local headers
  if ! headers="$(curl -fsS -D - -o /dev/null --max-time 5 "$BASE_URL/rpc/WiFi.GetStatus")"; then
    red "FAIL response headers: curl error"
    FAIL=1
    return
  fi
  local lower
  lower="$(printf '%s' "$headers" | tr '[:upper:]' '[:lower:]')"
  if ! printf '%s' "$lower" | grep -q '^server: shellyhttp/1.0.0'; then
    red "FAIL response headers: expected Server: ShellyHTTP/1.0.0"
    printf '%s\n' "$headers" | head -20
    FAIL=1
    return
  fi
  if ! printf '%s' "$lower" | grep -q '^connection: close'; then
    red "FAIL response headers: expected Connection: close"
    printf '%s' "$lower" | head -20
    echo
    FAIL=1
    return
  fi
  if printf '%s' "$lower" | grep -q '^date:'; then
    red "FAIL response headers: Date should be omitted (real ShellyHTTP)"
    printf '%s' "$lower" | head -20
    echo
    FAIL=1
    return
  fi
  green "OK   response headers"
}

check_response_headers

if [[ "$FAIL" -ne 0 ]]; then
  red "Validation FAILED against $BASE_URL"
  exit 1
fi
green "All checks passed against $BASE_URL"
