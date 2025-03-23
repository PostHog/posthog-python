from dataclasses import dataclass
from typing import List, TypedDict, Any, TypeAlias, cast

FlagValue: TypeAlias = bool | str

@dataclass(frozen=True)
class FlagReason:
    code: str
    condition_index: int
    description: str

    @classmethod
    def from_json(cls, resp: Any) -> "FlagReason":
        if not resp:
            return None
        return cls(
            code=resp.get("code", ""),
            condition_index=resp.get("condition_index", 0),
            description=resp.get("description", "")
        )


@dataclass(frozen=True)
class FlagMetadata:
    id: int
    payload: Any
    version: int
    description: str

    @classmethod
    def from_json(cls, resp: Any) -> "FlagMetadata":
        if not resp:
            return None
        return cls(
            id=resp.get("id", 0),
            payload=resp.get("payload"),
            version=resp.get("version", 0),
            description=resp.get("description", "")
        )

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
    def from_json(cls, resp: Any) -> "FeatureFlag":
        reason = None
        if resp.get("reason"):
            reason = FlagReason.from_json(resp.get("reason"))
        
        metadata = None
        if resp.get("metadata"):
            metadata = FlagMetadata.from_json(resp.get("metadata"))
        else:
            metadata = LegacyFlagMetadata(payload=None)

        return cls(
            key=resp.get("key"),
            enabled=resp.get("enabled"),
            variant=resp.get("variant"),
            reason=reason,
            metadata=metadata
        )

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
    if "flags" in resp:
        flags = resp["flags"]
        # For each flag, create a FeatureFlag object
        for key, value in flags.items():
            if isinstance(value, FeatureFlag):
                continue
            value["key"] = key
            flags[key] = FeatureFlag.from_json(value)
    else:
        # Handle legacy format
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

def to_flags_and_payloads(resp: DecideResponse) -> FlagsAndPayloads:
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

def to_values(response: DecideResponse) -> dict[str, FlagValue] | None:
    if "flags" not in response:
        return None

    flags = response.get("flags", {})
    return {key: value.get_value() for key, value in flags.items() if isinstance(value, FeatureFlag)}

def to_payloads(response: DecideResponse) -> dict[str, str] | None:
    if "flags" not in response:
        return None

    return {key: value.metadata.payload for key, value in response.get("flags", {}).items() if isinstance(value, FeatureFlag) and value.enabled}
