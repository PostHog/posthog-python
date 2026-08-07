#!/usr/bin/env bash
# Validate the posthog.mcp suite against both generations of the `mcp` SDK.
#
# The 2026-07-28 spec ships as `mcp` 2.x, a breaking rewrite of the same PyPI
# package that can't coexist with 1.x in one venv. This builds two throwaway uv
# venvs (v1 and v2), runs the mcp test subset in each, and prints a PASS/FAIL
# matrix. Non-interactive; exits non-zero if either env fails.
#
# Usage: scripts/validate-mcp-matrix.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

V1_ENV="$WORKDIR/v1"
V2_ENV="$WORKDIR/v2"
PYTHON_VERSION="3.12"

run_env() {
    # $1 = label, $2 = venv path, $3 = mcp spec to install over the base env
    local label="$1" env_path="$2" mcp_spec="$3"
    echo "=== [$label] building venv ($mcp_spec) ==="
    uv venv "$env_path" --python "$PYTHON_VERSION" >/dev/null || return 1
    UV_PROJECT_ENVIRONMENT="$env_path" uv sync --extra test >/dev/null || return 1
    UV_PROJECT_ENVIRONMENT="$env_path" uv pip install --python "$env_path/bin/python" "$mcp_spec" >/dev/null || return 1
    echo "--- [$label] installed mcp: $("$env_path/bin/python" -c 'from importlib.metadata import version; print(version("mcp"))')"
    echo "=== [$label] running posthog/test/mcp ==="
    "$env_path/bin/python" -m pytest posthog/test/mcp --timeout=30 -q
}

run_env "mcp-v1" "$V1_ENV" "mcp>=1.28.1,<2"
V1_STATUS=$?

run_env "mcp-v2" "$V2_ENV" "mcp>=2,<3"
V2_STATUS=$?

result() { [ "$1" -eq 0 ] && echo "PASS" || echo "FAIL"; }

echo ""
echo "================ MCP version matrix ================"
printf "  %-10s %s\n" "mcp 1.x" "$(result "$V1_STATUS")"
printf "  %-10s %s\n" "mcp 2.x" "$(result "$V2_STATUS")"
echo "===================================================="

[ "$V1_STATUS" -eq 0 ] && [ "$V2_STATUS" -eq 0 ]
