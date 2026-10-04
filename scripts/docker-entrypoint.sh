#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 W. Gentine
set -e

if [ "$(id -u)" = "0" ]; then
  # Named volumes keep ownership from older root-owned images; ensure /data is writable.
  chown -R app:app /data
  if capsh --has-p=cap_net_bind_service 2>/dev/null; then
    exec setpriv --reuid=app --regid=app --init-groups \
      --inh-caps=+net_bind_service --ambient-caps=+net_bind_service \
      -- "$@"
  fi
  exec setpriv --reuid=app --regid=app --init-groups -- "$@"
fi

exec "$@"
