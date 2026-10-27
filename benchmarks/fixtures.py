"""Deterministic fixtures matching the Node pilot's flag mix and identities."""

import hashlib
import json
from typing import Any

CASES = [
    "boolean",
    "rollout",
    "variant",
    "property",
    "regex",
    "cohort",
    "early",
    "late",
    "no-match",
    "inactive",
]
USERS = [
    {
        "distinct_id": f"benchmark-user-{i}",
        "person_properties": {
            "plan": "free" if i % 2 else "pro",
            "email": f"user-{i}@example.test",
            "score": i,
        },
    }
    for i in range(16)
]


def prop(key, value, operator="exact"):
    return {"key": key, "value": value, "operator": operator, "type": "person"}


def group(properties=None, rollout=100):
    return {"properties": properties or [], "rollout_percentage": rollout}


def definitions(count, generation=0):
    flags = []
    for i in range(count):
        kind = CASES[i % len(CASES)]
        match = group([prop("plan", ["pro"])])
        miss = group([prop("plan", ["never"])])
        groups = [group()]
        if kind == "rollout":
            groups = [group(rollout=50)]
        elif kind == "property":
            groups = [match]
        elif kind == "regex":
            groups = [
                group(
                    [
                        prop(
                            "email",
                            f"^(user|refresh-{generation})-[0-9]+@example[.]test$",
                            "regex",
                        )
                    ]
                )
            ]
        elif kind == "cohort":
            groups = [group([{"key": "id", "value": 1, "type": "cohort"}])]
        elif kind == "early":
            groups = [match] + [miss] * 7
        elif kind == "late":
            groups = [miss] * 7 + [match]
        elif kind == "no-match":
            groups = [miss] * 8
        filters: dict[str, Any] = {"groups": groups}
        if kind == "variant":
            filters["multivariate"] = {
                "variants": [
                    {"key": "control", "rollout_percentage": 50},
                    {"key": "test", "rollout_percentage": 50},
                ]
            }
        flags.append(
            {
                "id": i + 1,
                "key": f"{kind}-{i}",
                "name": f"{kind}-{i}",
                "active": kind != "inactive"
                and (kind != "boolean" or generation % 2 == 0),
                "filters": filters,
            }
        )
    return {
        "flags": flags,
        "group_type_mapping": {},
        "cohorts": {
            "1": {
                "type": "AND",
                "values": [
                    prop("plan", ["pro"]),
                    prop("score", 100 if generation % 2 else -1, "gt"),
                ],
            },
        },
    }


def bucket(key, distinct_id, salt=""):
    return int(
        hashlib.sha1(f"{key}.{distinct_id}{salt}".encode()).hexdigest()[:15], 16
    ) / (2**60 - 1)


def expected_value(index, user, generation=0):
    kind = CASES[index % len(CASES)]
    key = f"{kind}-{index}"
    if kind == "rollout":
        return bucket(key, user["distinct_id"]) < 0.5
    if kind == "variant":
        return (
            "control" if bucket(key, user["distinct_id"], "variant") < 0.5 else "test"
        )
    if kind == "boolean":
        return generation % 2 == 0
    if kind == "cohort" and generation % 2:
        return False
    if kind in ("property", "cohort", "early", "late"):
        return user["person_properties"]["plan"] == "pro"
    return kind not in ("inactive", "no-match")


def expected_flags(count, user, generation=0):
    return {
        f"{CASES[i % len(CASES)]}-{i}": expected_value(i, user, generation)
        for i in range(count)
    }


def digest(value):
    data = (
        value
        if isinstance(value, bytes)
        else json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    )
    return hashlib.sha256(data).hexdigest()


def workloads(smoke=False):
    return [
        *[
            {
                "id": f"flags.single.{kind}",
                "mode": "single",
                "flag_count": 10,
                "flag_index": i,
                "operations": 160 if smoke else 16000,
                "allocation_operations": 160 if smoke else 2000,
                "concurrency": 1,
                "passes": ["timing", "allocation"],
            }
            for i, kind in enumerate(CASES[:9])
        ],
        *[
            {
                "id": f"flags.all.{count}",
                "mode": "all",
                "flag_count": count,
                "operations": 16 if smoke else max(160, 16000 // count),
                "allocation_operations": 16 if smoke else max(16, 1600 // count),
                "concurrency": 1,
                "passes": ["timing", "allocation"],
            }
            for count in (10, 100, 1000)
        ],
        {
            "id": "flags.load.1000",
            "mode": "all",
            "flag_count": 1000,
            "operations": 32 if smoke else 1600,
            "concurrency": 8,
            "passes": ["memory"],
        },
        {
            "id": "flags.refresh.1000",
            "mode": "refresh",
            "flag_count": 1000,
            "operations": 4 if smoke else 40,
            "concurrency": 1,
            "passes": ["memory"],
        },
        *[
            {
                "id": f"capture.enqueue.{size}",
                "mode": "capture",
                "payload_bytes": size,
                "operations": 100 if smoke else 1000,
                "concurrency": 1,
                "passes": ["timing"],
            }
            for size in (32, 4096)
        ],
        *[
            {
                "id": f"capture.drain.{size}",
                "mode": "drain",
                "payload_bytes": size,
                "operations": 200 if smoke else 1000,
                "concurrency": 1,
                "passes": ["timing", "allocation", "memory"],
            }
            for size in (32, 4096)
        ],
    ]
