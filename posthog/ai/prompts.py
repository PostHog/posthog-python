"""
Prompt management for PostHog AI SDK.

Fetch and compile LLM prompts from PostHog with caching and fallback support.
"""

import copy
import logging
import re
import time
import urllib.parse
import warnings
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Union, overload

from posthog.request import USER_AGENT, _get_session
from posthog.utils import remove_trailing_slash

log = logging.getLogger("posthog")

APP_ENDPOINT = "https://us.posthog.com"
DEFAULT_CACHE_TTL_SECONDS = 300  # 5 minutes
# Backstop against a server whose pagination never terminates. 100 pages of the
# default page size covers 10,000 prompts.
_MAX_PROMPT_LIST_PAGES = 100

PromptVariables = Dict[str, Union[str, int, float, bool]]
PromptCacheKey = tuple[str, Optional[int], Optional[str]]

PromptSource = Literal["api", "cache", "stale_cache", "code_fallback"]


@dataclass(frozen=True)
class PromptResult:
    """Result of a prompt fetch with metadata about its source.

    ``label`` is the label the prompt resolved through, populated from the API
    response when fetching with the ``label`` option; ``None`` otherwise.

    ``config`` is the JSON object of model parameters or agent configuration
    stored with the prompt version, or ``None`` when the version has none
    (including on ``code_fallback`` results). Use defensive access, e.g.
    ``(result.config or {}).get("temperature", 0)``.
    """

    source: PromptSource
    prompt: str
    name: Optional[str] = None
    version: Optional[int] = None
    label: Optional[str] = None
    config: Optional[Dict[str, Any]] = None


class CachedPrompt:
    """Cached prompt with metadata."""

    def __init__(
        self,
        prompt: str,
        fetched_at: float,
        name: str,
        version: int,
        label: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        self.prompt = prompt
        self.fetched_at = fetched_at
        self.name = name
        self.version = version
        self.label = label
        self.config = config


def _cache_key(
    name: str, version: Optional[int], label: Optional[str] = None
) -> PromptCacheKey:
    """Build a cache key for latest, versioned, or labeled prompt fetches."""
    return (name, version, label)


def _prompt_reference(
    name: str,
    version: Optional[int],
    label: Optional[str] = None,
    *,
    capitalize: bool = False,
) -> str:
    """Format a prompt reference for logs and errors."""
    prefix = "Prompt" if capitalize else "prompt"
    reference = f'{prefix} "{name}"'
    if version is not None:
        return f"{reference} version {version}"
    if label is not None:
        return f'{reference} label "{label}"'
    return reference


def _extract_config(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Read config from an API response, tolerating servers that don't send it."""
    config = data.get("config")
    return config if isinstance(config, dict) else None


def _is_prompt_api_response(data: Any) -> bool:
    """Check if the response is a valid prompt API response."""
    return (
        isinstance(data, dict)
        and isinstance(data.get("prompt"), str)
        and isinstance(data.get("name"), str)
        and type(data.get("version")) is int
    )


def _row_resolves_label(row: Dict[str, Any], label: str) -> bool:
    """Check that the server resolved this list row through the requested label.

    An older server ignores the label param on the list endpoint and returns the
    latest version of every prompt. A row that was resolved through a label
    carries a matching name and version entry in its all_labels field.
    """
    all_labels = row.get("all_labels")
    if not isinstance(all_labels, list):
        return False
    return any(
        isinstance(entry, dict)
        and entry.get("name") == label
        and entry.get("version") == row.get("version")
        for entry in all_labels
    )


def _is_same_origin(url: str, host: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    expected = urllib.parse.urlsplit(host)
    return (parsed.scheme, parsed.netloc) == (expected.scheme, expected.netloc)


def _authentication_error(reference: str) -> Exception:
    return Exception(
        f"[PostHog Prompts] Authentication failed for {reference}. "
        "The key may be missing, expired, or the wrong type. Prompt fetches "
        "require a personal API key (starts with 'phx_'); a project secret "
        "key is not accepted. Pass this key as personal_api_key when you "
        "construct Prompts directly, or as secret_key when you configure the "
        "PostHog client."
    )


def _access_denied_error(reference: str) -> Exception:
    return Exception(
        f"[PostHog Prompts] Access denied for {reference}. "
        "Check that your personal_api_key has the correct permissions and the LLM prompts feature is enabled."
    )


class Prompts:
    """
    Fetch and compile LLM prompts from PostHog.

    Can be initialized with a PostHog client or with direct options.

    Examples:
        ```python
        from posthog import Posthog
        from posthog.ai.prompts import Prompts

        # With PostHog client
        posthog = Posthog('phc_xxx', host='https://us.posthog.com', secret_key='phx_xxx')
        prompts = Prompts(posthog)

        # Or with direct options (no PostHog client needed)
        prompts = Prompts(
            personal_api_key='phx_xxx',
            project_api_key='phc_xxx',
            host='https://us.posthog.com',
        )

        # With error tracking: prompt fetch failures are reported to PostHog
        prompts = Prompts(posthog, capture_errors=True)

        # Fetch with caching and fallback
        template = prompts.get('support-system-prompt', fallback='You are a helpful assistant.')

        # Fetch a specific published version
        prompt_v1 = prompts.get('support-system-prompt', version=1)

        # Fetch the version a label currently points to
        prod_prompt = prompts.get('support-system-prompt', label='production')

        # Fetch all prompts at a label in one request and warm the cache
        prod_prompts = prompts.get_all(label='production')

        # Compile with variables
        system_prompt = prompts.compile(template, {
            'company': 'Acme Corp',
            'tier': 'premium',
        })
        ```
    """

    def __init__(
        self,
        posthog: Optional[Any] = None,
        *,
        personal_api_key: Optional[str] = None,
        project_api_key: Optional[str] = None,
        host: Optional[str] = None,
        default_cache_ttl_seconds: Optional[int] = None,
        capture_errors: bool = False,
    ):
        """
        Initialize Prompts.

        Args:
            posthog: PostHog client instance (optional if personal_api_key provided)
            personal_api_key: Direct personal API key (optional if posthog provided)
            project_api_key: Direct project API key (optional if posthog provided)
            host: PostHog host (defaults to app endpoint)
            default_cache_ttl_seconds: Default cache TTL (defaults to 300)
            capture_errors: If True and a PostHog client is provided, prompt fetch
                failures are reported to PostHog error tracking via capture_exception().
        """
        self._default_cache_ttl_seconds = (
            default_cache_ttl_seconds
            if default_cache_ttl_seconds is not None
            else DEFAULT_CACHE_TTL_SECONDS
        )
        self._cache: Dict[PromptCacheKey, CachedPrompt] = {}
        self._has_warned_deprecation = False
        self._client = posthog
        self._capture_errors = capture_errors

        if posthog is not None:
            self._personal_api_key = getattr(posthog, "personal_api_key", None) or ""
            self._project_api_key = getattr(posthog, "api_key", None) or ""
            self._host = remove_trailing_slash(
                getattr(posthog, "raw_host", None) or APP_ENDPOINT
            )
        else:
            self._personal_api_key = personal_api_key or ""
            self._project_api_key = project_api_key or ""
            self._host = remove_trailing_slash(host or APP_ENDPOINT)

    @overload
    def get(
        self,
        name: str,
        *,
        with_metadata: Literal[True],
        cache_ttl_seconds: Optional[int] = ...,
        fallback: Optional[str] = ...,
        version: Optional[int] = ...,
        label: Optional[str] = ...,
    ) -> PromptResult: ...

    @overload
    def get(
        self,
        name: str,
        *,
        with_metadata: Literal[False],
        cache_ttl_seconds: Optional[int] = ...,
        fallback: Optional[str] = ...,
        version: Optional[int] = ...,
        label: Optional[str] = ...,
    ) -> str: ...

    @overload
    def get(
        self,
        name: str,
        *,
        cache_ttl_seconds: Optional[int] = ...,
        fallback: Optional[str] = ...,
        version: Optional[int] = ...,
        label: Optional[str] = ...,
    ) -> str: ...

    def get(
        self,
        name: str,
        *,
        with_metadata: Optional[bool] = None,
        cache_ttl_seconds: Optional[int] = None,
        fallback: Optional[str] = None,
        version: Optional[int] = None,
        label: Optional[str] = None,
    ) -> Union[str, PromptResult]:
        """
        Fetch a prompt by name from the PostHog API.

        When ``with_metadata`` is ``True``, returns a :class:`PromptResult`
        with ``source``, ``name``, ``version``, and ``config`` metadata.  When
        omitted or ``False``, returns a plain string (deprecated -- will be
        removed in a future major version).

        Args:
            name: The name of the prompt to fetch
            with_metadata: If True, returns a PromptResult with source info.
                Omitting this parameter is deprecated.
            cache_ttl_seconds: Cache TTL in seconds (defaults to instance default)
            fallback: Fallback prompt to use if fetch fails and no cache available
            version: Specific prompt version to fetch. Mutually exclusive with label.
                If neither is given, fetches the latest version
            label: Fetch the version this label currently points to, e.g.
                'production'. Mutually exclusive with version

        Returns:
            str if with_metadata is False/omitted, PromptResult if True

        Raises:
            ValueError: If both version and label are provided
            Exception: If the prompt cannot be fetched and no fallback is available
        """
        if version is not None and label is not None:
            raise ValueError(
                "[PostHog Prompts] Pass either version or label, not both."
            )
        if with_metadata is None and not self._has_warned_deprecation:
            self._has_warned_deprecation = True
            warnings.warn(
                "[PostHog Prompts] Calling get() without with_metadata=True is "
                "deprecated and will be removed in a future major version. "
                "Pass with_metadata=True to receive a PromptResult object with "
                "source, name, and version metadata. You can pass "
                "with_metadata=False to silence this warning, but the "
                "plain-string return will still be removed in the next major "
                "version.",
                DeprecationWarning,
                stacklevel=2,
            )

        try:
            result = self._get_internal(
                name, cache_ttl_seconds=cache_ttl_seconds, version=version, label=label
            )
            if with_metadata is True:
                return result
            return result.prompt
        except Exception as error:
            prompt_reference = _prompt_reference(name, version, label)
            if fallback is not None:
                log.warning(
                    "[PostHog Prompts] Failed to fetch %s, using fallback: %s",
                    prompt_reference,
                    error,
                )
                if with_metadata is True:
                    return PromptResult(source="code_fallback", prompt=fallback)
                return fallback
            raise

    def get_all(self, *, label: str) -> Dict[str, PromptResult]:
        """
        Fetch every prompt that carries a label, in one batch.

        Returns a dict mapping prompt name to :class:`PromptResult`, with each
        prompt at the version the label points to. Prompts without the label
        are not included.

        Each fetched prompt is stored in the cache, so later
        ``get(name, label=...)`` calls are served from cache within the TTL.
        An app with many prompts can call this once per cache cycle instead of
        making one ``get()`` request per prompt.

        Args:
            label: The label to resolve, e.g. 'production'.

        Returns:
            Dict of prompt name to PromptResult.

        Raises:
            Exception: If the request fails, or the server does not support
                fetching prompts by label on the list endpoint (PostHog
                releases from before September 2026).
        """
        try:
            rows = self._fetch_prompt_list_from_api(label)
        except Exception as error:
            self._maybe_capture_error(error, name="*", version=None, label=label)
            raise

        now = time.time()
        results: Dict[str, PromptResult] = {}
        skipped: List[str] = []
        for row in rows:
            if not _is_prompt_api_response(row) or not _row_resolves_label(row, label):
                skipped.append(str(row.get("name")) if isinstance(row, dict) else "?")
                continue

            config = _extract_config(row)
            self._cache[_cache_key(row["name"], None, label)] = CachedPrompt(
                prompt=row["prompt"],
                fetched_at=now,
                name=row["name"],
                version=row["version"],
                label=label,
                config=config,
            )
            results[row["name"]] = PromptResult(
                source="api",
                prompt=row["prompt"],
                name=row["name"],
                version=row["version"],
                label=label,
                config=copy.deepcopy(config),
            )

        if rows and not results:
            # Nothing resolved the label, so the server most likely ignored the
            # label param and served latest versions. Caching those under the
            # label would be the silent wrong-version failure labels exist to
            # prevent, so fail loudly instead.
            raise Exception(
                f'[PostHog Prompts] The server returned prompts, but none resolve label "{label}". '
                "It may not support fetching prompts by label on the list endpoint yet. "
                "Upgrade PostHog, or fetch prompts one by one with get()."
            )

        if skipped:
            log.warning(
                "[PostHog Prompts] Skipped %d prompt(s) that did not resolve label %r: %s",
                len(skipped),
                label,
                ", ".join(skipped),
            )

        return results

    def _get_internal(
        self,
        name: str,
        *,
        cache_ttl_seconds: Optional[int] = None,
        version: Optional[int] = None,
        label: Optional[str] = None,
    ) -> PromptResult:
        """
        Internal method that handles cache + fetch logic, returning full metadata.

        Does NOT handle the string ``fallback`` option -- the caller handles that.
        """
        ttl = (
            cache_ttl_seconds
            if cache_ttl_seconds is not None
            else self._default_cache_ttl_seconds
        )
        cache_key = _cache_key(name, version, label)

        # Check cache first
        cached = self._cache.get(cache_key)
        now = time.time()

        if cached is not None:
            is_fresh = (now - cached.fetched_at) < ttl

            if is_fresh:
                return PromptResult(
                    source="cache",
                    prompt=cached.prompt,
                    name=cached.name,
                    version=cached.version,
                    label=cached.label,
                    # Copied so a caller mutating result.config can't pollute the
                    # cache entry that later cache hits are served from.
                    config=copy.deepcopy(cached.config),
                )

        # Try to fetch from API
        try:
            data = self._fetch_prompt_from_api(name, version, label)

            # An older PostHog server ignores the label param and returns the latest
            # version with no label field — surface that instead of failing silently.
            if label is not None and data.get("label") != label:
                log.warning(
                    "[PostHog Prompts] Requested label %r for prompt %r but the server "
                    "resolved %r. It may not support prompt labels yet and returned the "
                    "latest version instead.",
                    label,
                    name,
                    data.get("label"),
                )

            config = _extract_config(data)

            # Update cache
            self._cache[cache_key] = CachedPrompt(
                prompt=data["prompt"],
                fetched_at=time.time(),
                name=data["name"],
                version=data["version"],
                label=data.get("label"),
                config=config,
            )

            return PromptResult(
                source="api",
                prompt=data["prompt"],
                name=data["name"],
                version=data["version"],
                label=data.get("label"),
                config=copy.deepcopy(config),
            )

        except Exception as error:
            self._maybe_capture_error(error, name=name, version=version, label=label)

            prompt_reference = _prompt_reference(name, version, label)
            # Return stale cache (with warning)
            if cached is not None:
                log.warning(
                    "[PostHog Prompts] Failed to fetch %s, using stale cache: %s",
                    prompt_reference,
                    error,
                )
                return PromptResult(
                    source="stale_cache",
                    prompt=cached.prompt,
                    name=cached.name,
                    version=cached.version,
                    label=cached.label,
                    config=copy.deepcopy(cached.config),
                )

            raise

    def compile(self, prompt: str, variables: PromptVariables) -> str:
        """
        Replace {{variableName}} placeholders with values.

        Unmatched variables are left unchanged.
        Supports variable names with hyphens and dots (e.g., user-id, company.name).

        Args:
            prompt: The prompt template string
            variables: Object containing variable values

        Returns:
            The compiled prompt string
        """

        def replace_variable(match: re.Match) -> str:
            variable_name = match.group(1)

            if variable_name in variables:
                return str(variables[variable_name])

            return match.group(0)

        return re.sub(r"\{\{([\w.-]+)\}\}", replace_variable, prompt)

    def clear_cache(
        self, name: Optional[str] = None, *, version: Optional[int] = None
    ) -> None:
        """
        Clear cached prompts.

        Args:
            name: Specific prompt name to clear. If None, clears all cached prompts.
            version: Specific prompt version to clear. Requires name.
        """
        if version is not None and name is None:
            raise ValueError("'version' requires 'name' to be provided")

        if name is None:
            self._cache.clear()
            return

        if version is not None:
            self._cache.pop(_cache_key(name, version), None)
            return

        keys_to_clear = [key for key in self._cache if key[0] == name]
        for key in keys_to_clear:
            self._cache.pop(key, None)

    def _maybe_capture_error(
        self,
        error: Exception,
        *,
        name: str,
        version: Optional[int],
        label: Optional[str] = None,
    ) -> None:
        """Report a prompt fetch error to PostHog error tracking if enabled."""
        if not self._capture_errors or self._client is None:
            return
        if not hasattr(self._client, "capture_exception"):
            return
        try:
            self._client.capture_exception(
                error,
                properties={
                    "$lib_feature": "ai.prompts",
                    "prompt_name": name,
                    "prompt_version": version,
                    "prompt_label": label,
                    "posthog_host": self._host,
                },
            )
        except Exception:
            log.debug("[PostHog Prompts] Failed to capture exception to error tracking")

    def _require_credentials(self) -> None:
        if not self._personal_api_key:
            raise Exception(
                "[PostHog Prompts] personal_api_key is required to fetch prompts. "
                "Please provide it when initializing the Prompts instance."
            )
        if not self._project_api_key:
            raise Exception(
                "[PostHog Prompts] project_api_key is required to fetch prompts. "
                "Please provide it when initializing the Prompts instance."
            )

    def _fetch_prompt_list_from_api(self, label: str) -> List[Dict[str, Any]]:
        """
        Fetch all prompts at a label from the paginated list endpoint.

        Endpoint:
            {host}/api/environments/@current/llm_prompts/
            ?token={encoded_project_api_key}&label={label}&content=full
        Auth: Bearer {personal_api_key}

        Follows pagination links until the last page. Returns the raw rows.
        """
        self._require_credentials()

        query = urllib.parse.urlencode(
            {"token": self._project_api_key, "label": label, "content": "full"}
        )
        url: Optional[str] = (
            f"{self._host}/api/environments/@current/llm_prompts/?{query}"
        )
        reference = f'prompts with label "{label}"'
        headers = {
            "Authorization": f"Bearer {self._personal_api_key}",
            "User-Agent": USER_AGENT,
        }

        rows: List[Dict[str, Any]] = []
        pages = 0
        while url:
            if pages >= _MAX_PROMPT_LIST_PAGES:
                log.warning(
                    "[PostHog Prompts] Stopped following pagination for %s after %d pages. "
                    "The result may be incomplete.",
                    reference,
                    pages,
                )
                break

            response = _get_session().get(url, headers=headers, timeout=10)

            if not response.ok:
                if response.status_code == 401:
                    raise _authentication_error(reference)
                if response.status_code == 403:
                    raise _access_denied_error(reference)
                raise Exception(
                    f"[PostHog Prompts] Failed to fetch {reference}: HTTP {response.status_code}"
                )

            try:
                data = response.json()
            except Exception:
                raise Exception(
                    f"[PostHog Prompts] Invalid response format for {reference}"
                )

            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise Exception(
                    f"[PostHog Prompts] Invalid response format for {reference}"
                )

            rows.extend(data["results"])

            # The Authorization header goes to every followed link, so a link
            # off the configured host must never be requested.
            next_url = data.get("next")
            if next_url is not None and (
                not isinstance(next_url, str)
                or not _is_same_origin(next_url, self._host)
            ):
                raise Exception(
                    f"[PostHog Prompts] Refusing to follow a pagination link off the "
                    f"configured host while fetching {reference}."
                )
            url = next_url
            pages += 1

        return rows

    def _fetch_prompt_from_api(
        self, name: str, version: Optional[int] = None, label: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Fetch prompt from PostHog API.

        Endpoint:
            {host}/api/environments/@current/llm_prompts/name/{encoded_name}/
            ?token={encoded_project_api_key}[&version={version}][&label={label}]
        Auth: Bearer {personal_api_key}

        Args:
            name: The name of the prompt to fetch
            version: Specific prompt version to fetch
            label: Fetch the version this label points to. If neither version nor
                label is given, fetches the latest

        Returns:
            The validated API response dict containing prompt, name, version,
            and label (when fetched by label)

        Raises:
            Exception: If the prompt cannot be fetched
        """
        self._require_credentials()

        encoded_name = urllib.parse.quote(name, safe="")
        query_params: Dict[str, Union[str, int]] = {"token": self._project_api_key}
        if version is not None:
            query_params["version"] = version
        if label is not None:
            query_params["label"] = label
        encoded_query = urllib.parse.urlencode(query_params)
        url = f"{self._host}/api/environments/@current/llm_prompts/name/{encoded_name}/?{encoded_query}"
        prompt_reference = _prompt_reference(name, version, label)
        prompt_title = _prompt_reference(name, version, label, capitalize=True)

        headers = {
            "Authorization": f"Bearer {self._personal_api_key}",
            "User-Agent": USER_AGENT,
        }

        response = _get_session().get(url, headers=headers, timeout=10)

        if not response.ok:
            if response.status_code == 404:
                raise Exception(f"[PostHog Prompts] {prompt_title} not found")

            if response.status_code == 401:
                raise _authentication_error(prompt_reference)

            if response.status_code == 403:
                raise _access_denied_error(prompt_reference)

            raise Exception(
                f"[PostHog Prompts] Failed to fetch {prompt_title}: HTTP {response.status_code}"
            )

        try:
            data = response.json()
        except Exception:
            raise Exception(
                f"[PostHog Prompts] Invalid response format for {prompt_title}"
            )

        if not _is_prompt_api_response(data):
            raise Exception(
                f"[PostHog Prompts] Invalid response format for {prompt_title}"
            )

        return data
