Feature: SDK utility behavior
  The SDK utility module prepares user data, cache entries, and runtime metadata
  for higher-level SDK flows.

  Scenario: Clean SDK payload values before capture
    Given an SDK event payload with Python-specific values
    When the SDK cleans the event payload
    Then transformed values are safe for SDK serialization
    And unsupported payload values are dropped to null

  Scenario: Reuse cached feature flag evaluations safely
    Given a cached feature flag evaluation for a user
    When the SDK reads the cached flag for current and newer definitions
    Then the current flag definition uses the cached evaluation
    And the newer flag definition misses the cache
    When the old flag definition is invalidated
    Then the cached evaluation is removed

  Scenario: Build runtime system context
    Given the SDK is running on a Linux host with distribution metadata
    When the SDK builds system context
    Then the context includes Python runtime and Linux metadata
