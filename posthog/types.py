from dataclasses import dataclass
from typing import Dict, List, TypedDict, Any


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

    def get_value(self):
        assert self.variant is None or self.enabled
        return self.variant or self.enabled

    @classmethod
    def from_value_and_payload(cls, key: str, value: any, payload: any) -> "FeatureFlag":
        enabled, variant = (True, value) if isinstance(value, str) else (value, None)
        return cls(
            key=key,
            enabled=enabled,
            variant=variant,
            reason=None,
            metadata=LegacyFlagMetadata(
                payload=payload,
            ),
        )


class DecideResponse(TypedDict, total=False):
    flags: Dict[str, FeatureFlag]
    errorsWhileComputingFlags: bool
    requestId: str
    quotaLimit: List[str] | None
