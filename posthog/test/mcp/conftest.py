"""Split the MCP test suite by installed MCP SDK major.

The suite runs against both majors in CI — the ``tests-mcp`` matrix has a
``mcp>=1.26,<2`` leg and a ``mcp>=2,<3`` leg (the main ``tests`` matrix also
exercises the v1 side incidentally). Files coupled to one
major's seams import symbols the other major doesn't ship, so they are excluded
from *collection* (a skip marker can't help — the failure is at import time).
Version-agnostic files (units, truncation, session tokens, PostHogMCP, ids)
collect under both majors.
"""

from posthog.test.mcp._helpers import MCP_MAJOR

_V1_ONLY = [
    # module-level `from mcp.server.fastmcp import ...` / v1 request_handlers seams
    "test_fastmcp.py",
    "test_fastmcp_v2.py",
    "test_features_m4.py",
    "test_lowlevel.py",
    "test_review_fixes.py",
]

_V2_ONLY = [
    # module-level `from mcp.server.mcpserver import ...` / v2 handler seams
    "test_v2_mcpserver.py",
    "test_v2_lowlevel.py",
    "test_v2_wire_dual_era.py",
]

collect_ignore = _V2_ONLY if MCP_MAJOR < 2 else _V1_ONLY
