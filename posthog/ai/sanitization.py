import re
from typing import Any, Optional

from posthog.ai.media import bytes_to_base64

REDACTED_IMAGE_PLACEHOLDER = "[base64 image redacted]"

_DATA_URL_RE = re.compile(r"^data:([^;,]+);base64,")
_BASE64_BODY_RE = re.compile(r"^[A-Za-z0-9+/_-]+={0,2}$")
_STRONG_CONTEXT_MIN_LEN = 200

_STRONG_KEYS = {
    "data",
    "image_url",
    "imageUrl",
    "video_url",
    "videoUrl",
    "audio",
    "audio_data",
    "audioData",
    "inline_data",
    "inlineData",
    "file_data",
    "fileData",
}
_MIME_HINT_KEYS = {"mime_type", "mimeType", "media_type", "mediaType", "format"}

# Container keys whose nested {"url": ...} value carries base64 media in the
# Chat Completions / LangChain dict shape ({"image_url": {"url": <b64>}}). The
# inner "url" key is weak on its own, so it's only strong when its parent dict
# sits under one of these.
_MEDIA_URL_CONTAINER_KEYS = {"image_url", "imageUrl", "video_url", "videoUrl"}


def _multimodal_capture_enabled(ph_client: Any = None) -> bool:
    """Media passthrough: on only when the client opted into multimodal capture."""
    return (
        getattr(ph_client, "_enable_multimodal_capture", False) is True
    )  # is True: tolerate unspecced Mock clients whose auto-generated attrs are truthy


_AUDIO_FORMATS = {"wav", "mp3", "pcm16", "pcm", "flac", "opus", "aac", "ogg", "m4a"}
_VIDEO_FORMATS = {"mp4", "mov", "webm", "avi", "mkv"}
_IMAGE_FORMATS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"}


_BARE_MEDIA_WORDS = {"image", "video", "audio"}


def _media_word(mime: Optional[str]) -> str:
    if not mime:
        return "image"
    if "/" in mime:
        for prefix in ("image", "video", "audio"):
            if mime.startswith(prefix + "/"):
                return prefix
        return "file"
    lowered = mime.lower()
    if lowered in _BARE_MEDIA_WORDS:
        return lowered
    if lowered in _AUDIO_FORMATS:
        return "audio"
    if lowered in _VIDEO_FORMATS:
        return "video"
    if lowered in _IMAGE_FORMATS:
        return "image"
    return "file"


def _placeholder(mime: Optional[str]) -> str:
    return f"[base64 {_media_word(mime)} redacted]"


def _sibling_mime(parent: Optional[dict]) -> Optional[str]:
    if not isinstance(parent, dict):
        return None
    for key in _MIME_HINT_KEYS:
        value = parent.get(key)
        if isinstance(value, str):
            return value
    return None


def _word_hint(parent: Optional[dict]) -> Optional[str]:
    mime = _sibling_mime(parent)
    if mime is not None:
        return mime
    if isinstance(parent, dict) and parent.get("type") in _BARE_MEDIA_WORDS:
        return parent["type"]
    return None


# Sibling-type hints: a key is strong context when its parent dict has one of
# these "type" values, without making the key itself universally strong (a
# bare "result" also shows up on tool outputs, where it must stay untouched).
_SIBLING_TYPE_STRONG_KEYS = {
    "result": {"image_generation_call"},
}


def _has_strong_sibling_type(key: Optional[str], parent: Optional[dict]) -> bool:
    if not isinstance(parent, dict):
        return False
    sibling_types = _SIBLING_TYPE_STRONG_KEYS.get(key or "")
    return sibling_types is not None and parent.get("type") in sibling_types


def _redact_string(
    value: str,
    key: Optional[str],
    parent: Optional[dict],
    parent_key: Optional[str],
) -> str:
    match = _DATA_URL_RE.match(value)
    if match:
        return _placeholder(match.group(1))
    strong = (
        (key in _STRONG_KEYS)
        or _has_strong_sibling_type(key, parent)
        or (key == "url" and parent_key in _MEDIA_URL_CONTAINER_KEYS)
    )
    if (
        strong
        and len(value) >= _STRONG_CONTEXT_MIN_LEN
        and _BASE64_BODY_RE.match(value)
    ):
        return _placeholder(_word_hint(parent))
    return value


def redact_media(
    value: Any, max_string_len: Optional[int] = None, ph_client: Any = None
) -> Any:
    passthrough = _multimodal_capture_enabled(ph_client)
    stack: set = set()

    def walk(
        node: Any,
        key: Optional[str],
        parent: Optional[dict],
        parent_key: Optional[str],
    ) -> Any:
        if isinstance(node, bytes):
            if passthrough:
                return bytes_to_base64(node)
            return _placeholder(_word_hint(parent))
        if isinstance(node, str):
            # Passthrough keeps media intact, so it must also skip truncation:
            # a 5000-char cut through base64 corrupts it as surely as redaction.
            if passthrough:
                return node
            out = _redact_string(node, key, parent, parent_key)
            if max_string_len is not None and len(out) > max_string_len:
                out = out[:max_string_len] + "... [truncated]"
            return out
        if isinstance(node, dict):
            if id(node) in stack:
                return None
            stack.add(id(node))
            try:
                return {k: walk(v, k, node, key) for k, v in node.items()}
            finally:
                stack.discard(id(node))
        if isinstance(node, (list, tuple)):
            if id(node) in stack:
                return None
            stack.add(id(node))
            try:
                return [walk(item, key, parent, parent_key) for item in node]
            finally:
                stack.discard(id(node))
        return node

    return walk(value, None, None, None)


def sanitize_messages(
    data: Any, provider: Optional[str] = None, ph_client: Any = None
) -> Any:
    """Back-compat entry point; provider is ignored — redaction is structural now."""
    return redact_media(data, ph_client=ph_client)


def redact_base64_data_url(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return _redact_string(value, "data", None, None)


# Back-compat wrappers for the old per-provider sanitizers — still imported by
# wrappers and langchain/callbacks.py; kept in the public API snapshot. They
# stay 2-arg-tolerant so callers threading the client keep working.
def sanitize_openai(data: Any, ph_client: Any = None) -> Any:
    return sanitize_messages(data, ph_client=ph_client)


def sanitize_openai_response(data: Any, ph_client: Any = None) -> Any:
    return sanitize_messages(data, ph_client=ph_client)


def sanitize_anthropic(data: Any, ph_client: Any = None) -> Any:
    return sanitize_messages(data, ph_client=ph_client)


def sanitize_gemini(data: Any, ph_client: Any = None) -> Any:
    return sanitize_messages(data, ph_client=ph_client)


def sanitize_langchain(data: Any, ph_client: Any = None) -> Any:
    return sanitize_messages(data, ph_client=ph_client)
