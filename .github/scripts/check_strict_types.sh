#!/usr/bin/env bash
set -euo pipefail

PYTHON_VERSION="${PYTHON_VERSION:-3.11}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

python -m venv "$tmp/.venv"
"$tmp/.venv/bin/python" -m pip install --quiet --upgrade pip
"$tmp/.venv/bin/python" -m pip install --quiet . pyright

cat > "$tmp/strict_posthog_types.py" <<'PY'
# pyright: strict
import atexit

import posthog
from posthog import FlagValue, Posthog

client = Posthog("phc_test")
atexit.register(client.shutdown)

flag_value: FlagValue | None = client.get_feature_flag("flag", "user")
all_flags: dict[str, FlagValue] | None = posthog.get_all_flags("user")
enabled: bool | None = posthog.feature_enabled("flag", "user")
payload: object | None = client.get_feature_flag_payload("flag", "user")

_ = (flag_value, all_flags, enabled, payload)
PY

cat > "$tmp/pyrightconfig.json" <<JSON
{
  "typeCheckingMode": "strict",
  "pythonVersion": "$PYTHON_VERSION",
  "venvPath": "$tmp",
  "venv": ".venv",
  "reportMissingTypeStubs": "error",
  "reportPrivateImportUsage": "error",
  "reportUnknownArgumentType": "error",
  "reportUnknownMemberType": "error",
  "reportUnknownVariableType": "error"
}
JSON

cd "$tmp"
"$tmp/.venv/bin/python" -m pyright strict_posthog_types.py
