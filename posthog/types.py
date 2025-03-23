from dataclasses import dataclass
from typing import List, TypedDict, Any, TypeAlias, cast

FlagValue: TypeAlias = bool | str

@dataclass(frozen=True)
class FlagReason(TypedDict):
    code: str
    condition_index: int
    description: str


@dataclass(frozen=True)
class FlagMetadata:
    id: int
    payload: Any
    version: int
    description: str


@dataclass(frozen=True)
class LegacyFlagMetadata:
    payload: Any


@dataclass(frozen=True)
class FeatureFlag:
    key: str
    enabled: bool
    variant: str | None
    reason: FlagReason | None
    metadata: FlagMetadata | LegacyFlagMetadata

    def get_value(self) -> FlagValue:
        assert self.variant is None or self.enabled
        return self.variant or self.enabled

    @classmethod
    def from_value_and_payload(cls, key: str, value: FlagValue, payload: Any) -> "FeatureFlag":
        enabled, variant = (True, value) if isinstance(value, str) else (value, None)
        return cls(
            key=key,
            enabled=enabled,
            variant=variant,
            reason=None,
            metadata=LegacyFlagMetadata(
                payload=payload if payload else None,
            ),
        )


class DecideResponse(TypedDict, total=False):
    flags: dict[str, FeatureFlag]
    errorsWhileComputingFlags: bool
    requestId: str
    quotaLimit: List[str] | None


class FlagsAndPayloads(TypedDict, total=True):
    featureFlags: dict[str, FlagValue] | None
    featureFlagPayloads: dict[str, Any] | None

def normalize_decide_response(resp: Any) -> DecideResponse:
    """
    Normalize the response from the decide API endpoint into a v4 DecideResponse.

    Args:
        resp: A v3 or v4 response from the decide API endpoint.

    Returns:
        A DecideResponse containing feature flags and their details.
    """
    if "requestId" not in resp:
        resp["requestId"] = None
    if "flags" not in resp:
        featureFlags = resp.get("featureFlags", {})
        featureFlagPayloads = resp.get("featureFlagPayloads", {})
        resp.pop("featureFlags", None)
        resp.pop("featureFlagPayloads", None)
        # look at each key in featureFlags and create a FeatureFlag object
        flags = {}
        for key, value in featureFlags.items():
            flags[key] = FeatureFlag.from_value_and_payload(key, value, featureFlagPayloads.get(key, None))
        resp["flags"] = flags
    return cast(DecideResponse, resp)

def to_flags_and_payloads(resp: DecideResponse) -> tuple[dict[str, FlagValue], dict[str, Any], bool]:
    """
    Convert a DecideResponse into a FlagsAndPayloads object which is a 
    dict of feature flags and their payloads. This is needed by certain 
    functions in the client.
    Args:
        resp: A DecideResponse containing feature flags and their payloads.

    Returns:
        A tuple containing:
            - A dictionary mapping flag keys to their values (bool or str)
            - A dictionary mapping flag keys to their payloads
    """
    return {
        "featureFlags": to_values(resp),
        "featureFlagPayloads": to_payloads(resp)
    }

def to_values(response: DecideResponse) -> dict[str, bool | str] | None:
    if "flags" not in response:
        return None

    return {key: value.get_value() for key, value in response.get("flags", {}).items()}

def to_payloads(response: DecideResponse) -> dict[str, str] | None:
    if "flags" not in response:
        return None

    return {key: value.metadata.payload for key, value in response.get("flags", {}).items() if value.enabled}
