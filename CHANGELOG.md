# 7.5.1 - 2026-01-07

fix: avoid return from finally block to fix Python 3.14 SyntaxWarning https://github.com/PostHog/posthog-python/pull/361 - thanks @jodal

# 7.5.0 - 2026-01-06

feat: Capture Langchain, OpenAI and Anthropic errors as exceptions (if exception autocapture is enabled)
feat: Add reference to exception in LLMA trace and span events

# 7.4.3 - 2026-01-02

Fixes cache creation cost for Langchain with Anthropic

# 7.4.2 - 2025-12-22

feat: add `in_app_modules` option to control code variables capturing

# 7.4.1 - 2025-12-19

fix: extract model from response for OpenAI stored prompts

When using OpenAI stored prompts, the model is defined in the OpenAI dashboard rather than passed in the API request. This fix adds a fallback to extract the model from the response object when not provided in kwargs, ensuring generations show up with the correct model and enabling cost calculations.

# 7.4.0 - 2025-12-16

feat: Add automatic retries for feature flag requests

Feature flag API requests now automatically retry on transient failures:

- Network errors (connection refused, DNS failures, timeouts)
- Server errors (500, 502, 503, 504)
- Up to 2 retries with exponential backoff (0.5s, 1s delays)

Rate limit (429) and quota (402) errors are not retried.

# 7.3.1 - 2025-12-06

fix: remove unused $exception_message and $exception_type

# 7.3.0 - 2025-12-05

feat: improve code variables capture masking

# 7.2.0 - 2025-12-01

feat: add $feature_flag_evaluated_at properties to $feature_flag_called events

# 7.1.0 - 2025-11-26

Add support for the async version of Gemini.

# 7.0.2 - 2025-11-18

Add support for Python 3.14.
Projects upgrading to Python 3.14 should ensure any Pydantic models passed into the SDK use Pydantic v2, as Pydantic v1 is not compatible with Python 3.14.

# 7.0.1 - 2025-11-15

Try to use repr() when formatting code variables

# 7.0.0 - 2025-11-11

NB Python 3.9 is no longer supported

- chore(llma): update LLM provider SDKs to latest major versions
  - openai: 1.102.0 → 2.7.1
  - anthropic: 0.64.0 → 0.72.0
  - google-genai: 1.32.0 → 1.49.0
  - langchain-core: 0.3.75 → 1.0.3
  - langchain-openai: 0.3.32 → 1.0.2
  - langchain-anthropic: 0.3.19 → 1.0.1
  - langchain-community: 0.3.29 → 0.4.1
  - langgraph: 0.6.6 → 1.0.2

# 6.9.3 - 2025-11-10

- feat(ph-ai): PostHog properties dict in GenerationMetadata

# 6.9.2 - 2025-11-10

- fix(llma): fix cache token double subtraction in Langchain for non-Anthropic providers causing negative costs

# 6.9.1 - 2025-11-07

- fix(error-tracking): pass code variables config from init to client

# 6.9.0 - 2025-11-06

- feat(error-tracking): add local variables capture

# 6.8.0 - 2025-11-03

- feat(llma): send web search calls to be used for LLM cost calculations

# 6.7.14 - 2025-11-03

- fix(django): Handle request.user access in async middleware context to prevent SynchronousOnlyOperation errors in Django 5+ (fixes #355)
- test(django): Add Django 5 integration test suite with real ASGI application testing async middleware behavior

# 6.7.13 - 2025-11-02

- fix(llma): cache cost calculation in the LangChain callback

# 6.7.12 - 2025-11-02

- fix(django): Restore process_exception method to capture view and downstream middleware exceptions (fixes #329)
- fix(ai/langchain): Add LangChain 1.0+ compatibility for CallbackHandler imports (fixes #362)

# 6.7.11 - 2025-10-28

- feat(ai): Add `$ai_framework` property for framework integrations (e.g. LangChain)

# 6.7.10 - 2025-10-24

- fix(django): Make middleware truly hybrid - compatible with both sync (WSGI) and async (ASGI) Django stacks without breaking sync-only deployments

# 6.7.9 - 2025-10-22

- fix(flags): multi-condition flags with static cohorts returning wrong variants

# 6.7.8 - 2025-10-16

- fix(llma): missing async for OpenAI's streaming implementation

# 6.7.7 - 2025-10-14

- fix: remove deprecated attribute $exception_personURL from exception events

# 6.7.6 - 2025-09-16

- fix: don't sort condition sets with variant overrides to the top
- fix: Prevent core Client methods from raising exceptions

# 6.7.5 - 2025-09-16

- feat: Django middleware now supports async request handling.

# 6.7.4 - 2025-09-05

- fix: Missing system prompts for some providers

# 6.7.3 - 2025-09-04

- fix: missing usage tokens in Gemini

# 6.7.2 - 2025-09-03

- fix: tool call results in streaming providers

# 6.7.1 - 2025-09-01

- fix: Add base64 inline image sanitization

# 6.7.0 - 2025-08-26

- feat: Add support for feature flag dependencies

# 6.6.1 - 2025-08-21

- fix: Prevent `NoneType` error when `group_properties` is `None`

# 6.6.0 - 2025-08-15

- feat: Add `flag_keys_to_evaluate` parameter to optimize feature flag evaluation performance by only evaluating specified flags
- feat: Add `flag_keys_filter` option to `send_feature_flags` for selective flag evaluation in capture events

# 6.5.0 - 2025-08-08

- feat: Add `$context_tags` to an event to know which properties were included as tags

# 6.4.1 - 2025-08-06

- fix: Always pass project API key in `remote_config` requests for deterministic project routing

# 6.4.0 - 2025-08-05

- feat: support Vertex AI for Gemini

# 6.3.4 - 2025-08-04

- fix: set `$ai_tools` for all providers and `$ai_output_choices` for all non-streaming provider flows properly

# 6.3.3 - 2025-08-01

- fix: `get_feature_flag_result` now correctly returns FeatureFlagResult when payload is empty string instead of None

# 6.3.2 - 2025-07-31

- fix: Anthropic's tool calls are now handled properly

# 6.3.0 - 2025-07-22

- feat: Enhanced `send_feature_flags` parameter to accept `SendFeatureFlagsOptions` object for declarative control over local/remote evaluation and custom properties

# 6.2.1 - 2025-07-21

- feat: make `posthog_client` an optional argument in PostHog AI providers wrappers (`posthog.ai.*`), intuitively using the default client as the default

# 6.1.1 - 2025-07-16

- fix: correctly capture exceptions processed by Django from views or middleware

# 6.1.0 - 2025-07-10

- feat: decouple feature flag local evaluation from personal API keys; support decrypting remote config payloads without relying on the feature flags poller

# 6.0.4 - 2025-07-09

- fix: add POSTHOG_MW_CLIENT setting to django middleware, to support custom clients for exception capture.

# 6.0.3 - 2025-07-07

- feat: add a feature flag evaluation cache (local storage or redis) to support returning flag evaluations when the service is down

# 6.0.2 - 2025-07-02

- fix: send_feature_flags changed to default to false in `Client::capture_exception`

# 6.0.1

- fix: response `$process_person_profile` property when passed to capture

# 6.0.0

This release contains a number of major breaking changes:

- feat: make distinct_id an optional parameter in posthog.capture and related functions
- feat: make capture and related functions return `Optional[str]`, which is the UUID of the sent event, if it was sent
- fix: remove `identify` (prefer `posthog.set()`), and `page` and `screen` (prefer `posthog.capture()`)
- fix: delete exception-capture specific integrations module. Prefer the general-purpose django middleware as a replacement for the django `Integration`.

To migrate to this version, you'll mostly just need to switch to using named keyword arguments, rather than positional ones. For example:

```python
# Old calling convention
posthog.capture("user123", "button_clicked", {"button_id": "123"})
# New calling convention
posthog.capture(distinct_id="user123", event="button_clicked", properties={"button_id": "123"})

# Better pattern
with posthog.new_context():
    posthog.identify_context("user123")

    # The event name is the first argument, and can be passed positionally, or as a keyword argument in a later position
    posthog.capture("button_pressed")
```

Generally, arguments are now appropriately typed, and docstrings have been updated. If something is unclear, please open an issue, or submit a PR!

# 5.4.0 - 2025-06-20

- feat: add support to session_id context on page method

# 5.3.0 - 2025-06-19

- fix: safely handle exception values

# 5.2.0 - 2025-06-19

- feat: construct artificial stack traces if no traceback is available on a captured exception

## 5.1.0 - 2025-06-18

- feat: session and distinct ID's can now be associated with contexts, and are used as such
- feat: django http request middleware

## 5.0.0 - 2025-06-16

- fix: removed deprecated sentry integration

## 4.10.0 - 2025-06-13

- fix: no longer fail in autocapture.

## 4.9.0 - 2025-06-13

- feat(ai): track reasoning and cache tokens in the LangChain callback

## 4.8.0 - 2025-06-10

- fix: export scoped, rather than tracked, decorator
- feat: allow use of contexts without error tracking

## 4.7.0 - 2025-06-10

- feat: add support for parse endpoint in responses API (no longer beta)

## 4.6.2 - 2025-06-09

- fix: replace `import posthog` with direct method imports

## 4.6.1 - 2025-06-09

- fix: replace `import posthog` in `posthoganalytics` package

## 4.6.0 - 2025-06-09

- feat: add additional user and request context to captured exceptions via the Django integration
- feat: Add `setup()` function to initialise default client

## 4.5.0 - 2025-06-09

- feat: add before_send callback (#249)

## 4.4.2- 2025-06-09

- empty point release to fix release automation

## 4.4.1 2025-06-09

- empty point release to fix release automation

## 4.4.0 - 2025-06-09

- Use the new `/flags` endpoint for all feature flag evaluations (don't fall back to `/decide` at all)

## 4.3.2 - 2025-06-06

1. Add context management:

- New context manager with `posthog.new_context()`
- Tag functions: `posthog.tag()`, `posthog.get_tags()`, `posthog.clear_tags()`
- Function decorator:
  - `@posthog.scoped` - Creates context and captures exceptions thrown within the function
- Automatic deduplication of exceptions to ensure each exception is only captured once

2. fix: feature flag request use geoip_disable (#235)
3. chore: pin actions versions (#210)
4. fix: opinionated setup and clean fn fix (#240)
5. fix: release action failed (#241)

## 4.2.0 - 2025-05-22

Add support for google gemini

## 4.1.0 - 2025-05-22

Moved ai openai package to a composition approach over inheritance.

## 4.0.1 – 2025-04-29

1. Remove deprecated `monotonic` library. Use Python's core `time.monotonic` function instead
2. Clarify Python 3.9+ is required

## 4.0.0 - 2025-04-24

1. Added new method `get_feature_flag_result` which returns a `FeatureFlagResult` object. This object breaks down the result of a feature flag into its enabled state, variant, and payload. The benefit of this method is it allows you to retrieve the result of a feature flag and its payload in a single API call. You can call `get_value` on the result to get the value of the feature flag, which is the same value returned by `get_feature_flag` (aka the string `variant` if the flag is a multivariate flag or the `boolean` value if the flag is a boolean flag).

Example:

```python
result = posthog.get_feature_flag_result("my-flag", "distinct_id")
print(result.enabled)     # True or False
print(result.variant)     # 'the-variant-value' or None
print(result.payload)     # {'foo': 'bar'}
print(result.get_value()) # 'the-variant-value' or True or False
print(result.reason)      # 'matched condition set 2' (Not available for local evaluation)
```

Breaking change:

1. `get_feature_flag_payload` now deserializes payloads from JSON strings to `Any`. Previously, it returned the payload as a JSON encoded string.

Before:

```python
payload = get_feature_flag_payload('key', 'distinct_id') # "{\"some\": \"payload\"}"
```

After:

```python
payload = get_feature_flag_payload('key', 'distinct_id') # {"some": "payload"}
```

## 3.25.0 – 2025-04-15

1. Roll out new `/flags` endpoint to 100% of `/decide` traffic, excluding the top 10 customers.

## 3.24.3 – 2025-04-15

1. Fix hash inclusion/exclusion for flag rollout

## 3.24.2 – 2025-04-15

1. Roll out new /flags endpoint to 10% of /decide traffic

## 3.24.1 – 2025-04-11

1. Add `log_captured_exceptions` option to proxy setup

## 3.24.0 – 2025-04-10

1. Add config option to `log_captured_exceptions`

## 3.23.0 – 2025-03-26

1. Expand automatic retries to include read errors (e.g. RemoteDisconnected)

## 3.22.0 – 2025-03-26

1. Add more information to `$feature_flag_called` events.
2. Support for the `/decide?v=4` endpoint which contains more information about feature flags.

## 3.21.0 – 2025-03-17

1. Support serializing dataclasses.

## 3.20.0 – 2025-03-13

1. Add support for OpenAI Responses API.

## 3.19.2 – 2025-03-11

1. Fix install requirements for analytics package

## 3.19.1 – 2025-03-11

1. Fix bug where None is sent as delta in azure

## 3.19.0 – 2025-03-04

1. Add support for tool calls in OpenAI and Anthropic.
2. Add support for cached tokens.

## 3.18.1 – 2025-03-03

1. Improve quota-limited feature flag logs

## 3.18.0 - 2025-02-28

1. Add support for Azure OpenAI.

## 3.17.0 - 2025-02-27

1. The LangChain handler now captures tools in `$ai_generation` events, in property `$ai_tools`. This allows for displaying tools provided to the LLM call in PostHog UI. Note that support for `$ai_tools` in OpenAI and Anthropic SDKs is coming soon.

## 3.16.0 - 2025-02-26

1. feat: add some platform info to events (#198)

## 3.15.1 - 2025-02-23

1. Fix async client support for OpenAI.

## 3.15.0 - 2025-02-19

1. Support quota-limited feature flags

## 3.14.2 - 2025-02-19

1. Evaluate feature flag payloads with case sensitivity correctly. Fixes <https://github.com/PostHog/posthog-python/issues/178>

## 3.14.1 - 2025-02-18

1. Add support for Bedrock Anthropic Usage

## 3.13.0 - 2025-02-12

1. Automatically retry connection errors

## 3.12.1 - 2025-02-11

1. Fix mypy support for 3.12.0
2. Deprecate `is_simple_flag`

## 3.12.0 - 2025-02-11

1. Add support for OpenAI beta parse API.
2. Deprecate `context` parameter

## 3.11.1 - 2025-02-06

1. Fix LangChain callback handler to capture parent run ID.

## 3.11.0 - 2025-01-28

1. Add the `$ai_span` event to the LangChain callback handler to capture the input and output of intermediary chains.

   > LLM observability naming change: event property `$ai_trace_name` is now `$ai_span_name`.

2. Fix serialiazation of Pydantic models in methods.

## 3.10.0 - 2025-01-24

1. Add `$ai_error` and `$ai_is_error` properties to LangChain callback handler, OpenAI, and Anthropic.

## 3.9.3 - 2025-01-23

1. Fix capturing of multiple traces in the LangChain callback handler.

## 3.9.2 - 2025-01-22

1. Fix importing of LangChain callback handler under certain circumstances.

## 3.9.0 - 2025-01-22

1. Add `$ai_trace` event emission to LangChain callback handler.

## 3.8.4 - 2025-01-17

1. Add Anthropic support for LLM Observability.
2. Update LLM Observability to use output_choices.

## 3.8.3 - 2025-01-14

1. Fix setuptools to include the `posthog.ai.openai` and `posthog.ai.langchain` packages for the `posthoganalytics` package.

## 3.8.2 - 2025-01-14

1. Fix setuptools to include the `posthog.ai.openai` and `posthog.ai.langchain` packages.

## 3.8.1 - 2025-01-14

1. Add LLM Observability with support for OpenAI and Langchain callbacks.

## 3.7.5 - 2025-01-03

1. Add `distinct_id` to group_identify

## 3.7.4 - 2024-11-25

1. Fix bug where this SDK incorrectly sent feature flag events with null values when calling `get_feature_flag_payload`.

## 3.7.3 - 2024-11-25

1. Use personless mode when sending an exception without a provided `distinct_id`.

## 3.7.2 - 2024-11-19

1. Add `type` property to exception stacks.

## 3.7.1 - 2024-10-24

1. Add `platform` property to each frame of exception stacks.

## 3.7.0 - 2024-10-03

1. Adds a new `super_properties` parameter on the client that are appended to every /capture call.

## 3.6.7 - 2024-09-24

1. Remove deprecated datetime.utcnow() in favour of datetime.now(tz=tzutc())

## 3.6.6 - 2024-09-16

1. Fix manual capture support for in app frames

## 3.6.5 - 2024-09-10

1. Fix django integration support for manual exception capture.

## 3.6.4 - 2024-09-05

1. Add manual exception capture.

## 3.6.3 - 2024-09-03

1. Make sure setup.py for posthoganalytics package also discovers the new exception integration package.

## 3.6.2 - 2024-09-03

1. Make sure setup.py discovers the new exception integration package.

## 3.6.1 - 2024-09-03

1. Adds django integration to exception autocapture in alpha state. This feature is not yet stable and may change in future versions.

## 3.6.0 - 2024-08-28

1. Adds exception autocapture in alpha state. This feature is not yet stable and may change in future versions.

## 3.5.2 - 2024-08-21

1. Guard for None values in local evaluation

## 3.5.1 - 2024-08-13

1. Remove "-api" suffix from ingestion hostnames

## 3.5.0 - 2024-02-29

1. - Adds a new `feature_flags_request_timeout_seconds` timeout parameter for feature flags which defaults to 3 seconds, updated from the default 10s for all other API calls.

## 3.4.2 - 2024-02-20

1. Add `historical_migration` option for bulk migration to PostHog Cloud.

## 3.4.1 - 2024-02-09

1. Use new hosts for event capture as well

## 3.4.0 - 2024-02-05

1. Point given hosts to new ingestion hosts

## 3.3.4 - 2024-01-30

1. Update type hints for module variables to work with newer versions of mypy

## 3.3.3 - 2024-01-26

1. Remove new relative date operators, combine into regular date operators

## 3.3.2 - 2024-01-19

1. Return success/failure with all capture calls from module functions

## 3.3.1 - 2024-01-10

1. Make sure we don't override any existing feature flag properties when adding locally evaluated feature flag properties.

## 3.3.0 - 2024-01-09

1. When local evaluation is enabled, we automatically add flag information to all events sent to PostHog, whenever possible. This makes it easier to use these events in experiments.

## 3.2.0 - 2024-01-09

1. Numeric property handling for feature flags now does the expected: When passed in a number, we do a numeric comparison. When passed in a string, we do a string comparison. Previously, we always did a string comparison.
2. Add support for relative date operators for local evaluation.

## 3.1.0 - 2023-12-04

1. Increase maximum event size and batch size

## 3.0.2 - 2023-08-17

1. Returns the current flag property with $feature_flag_called events, to make it easier to use in experiments

## 3.0.1 - 2023-04-21

1. Restore how feature flags work when the client library is disabled: All requests return `None` and no events are sent when the client is disabled.
2. Add a `feature_flag_definitions()` debug option, which returns currently loaded feature flag definitions. You can use this to more cleverly decide when to request local evaluation of feature flags.

## 3.0.0 - 2023-04-14

Breaking change:

All events by default now send the `$geoip_disable` property to disable geoip lookup in app. This is because usually we don't
want to update person properties to take the server's location.

The same now happens for feature flag requests, where we discard the IP address of the server for matching on geoip properties like city, country, continent.

To restore previous behaviour, you can set the default to False like so:

```python
posthog.disable_geoip = False

# // and if using client instantiation:
posthog = Posthog('api_key', disable_geoip=False)

```

## 2.5.0 - 2023-04-10

1. Add option for instantiating separate client object

## 2.4.2 - 2023-03-30

1. Update backoff dependency for posthoganalytics package to be the same as posthog package

## 2.4.1 - 2023-03-17

1. Removes accidental print call left in for decide response

## 2.4.0 - 2023-03-14

1. Support evaluating all cohorts in feature flags for local evaluation

## 2.3.1 - 2023-02-07

1. Log instead of raise error on posthog personal api key errors
2. Remove upper bound on backoff dependency

## 2.3.0 - 2023-01-31

1. Add support for returning payloads of matched feature flags

## 2.2.0 - 2022-11-14

Changes:

1. Add support for feature flag variant overrides with local evaluation

## 2.1.2 - 2022-09-15

Changes:

1. Fixes issues with date comparison.

## 2.1.1 - 2022-09-14

Changes:

1. Feature flags local evaluation now supports date property filters as well. Accepts both strings and datetime objects.

## 2.1.0 - 2022-08-11

Changes:

1. Feature flag defaults have been removed
2. Setup logging only when debug mode is enabled.

## 2.0.1 - 2022-08-04

- Make poll_interval configurable
- Add `send_feature_flag_events` parameter to feature flag calls, which determine whether the `$feature_flag_called` event should be sent or not.
- Add `only_evaluate_locally` parameter to feature flag calls, which determines whether the feature flag should only be evaluated locally or not.

## 2.0.0 - 2022-08-02

Breaking changes:

1. The minimum version requirement for PostHog servers is now 1.38. If you're using PostHog Cloud, you satisfy this requirement automatically.
2. Feature flag defaults apply only when there's an error fetching feature flag results. Earlier, if the default was set to `True`, even if a flag resolved to `False`, the default would override this.
   **Note: These are removed in 2.0.2**
3. Feature flag remote evaluation doesn't require a personal API key.

New Changes:

1. You can now evaluate feature flags locally (i.e. without sending a request to your PostHog servers) by setting a personal API key, and passing in groups and person properties to `is_feature_enabled` and `get_feature_flag` calls.
2. Introduces a `get_all_flags` method that returns all feature flags. This is useful for when you want to seed your frontend with some initial flags, given a user ID.

## 1.4.9 - 2022-06-13

- Support for sending feature flags with capture calls

## 1.4.8 - 2022-05-12

- Support multi variate feature flags

## 1.4.7 - 2022-04-25

- Allow feature flags usage without project_api_key

## 1.4.1 - 2021-05-28

- Fix packaging issues with Sentry integrations

## 1.4.0 - 2021-05-18

- Improve support for `project_api_key` (#32)
- Resolve polling issues with feature flags (#29)
- Add Sentry (and Sentry+Django) integrations (#13)
- Fix feature flag issue with no percentage rollout (#30)

## 1.3.1 - 2021-05-07

- Add `$set` and `$set_once` support (#23)
- Add distinct ID to `$create_alias` event (#27)
- Add `UUID` to `ID_TYPES` (#26)

## 1.2.1 - 2021-02-05

Initial release logged in CHANGELOG.md.
