Feature: SDK utility behavior
  The SDK utility module prepares user data, cache entries, and runtime metadata
  for higher-level SDK flows.

  Scenario Outline: Clean SDK payload values before capture
    Given an SDK payload value of type <value_type>
    When the SDK cleans the event payload
    Then the cleaned payload value equals <expected_json>

    Examples:
      | value_type  | expected_json                                  |
      | uuid        | "12345678-1234-5678-1234-567812345678"       |
      | decimal     | 12.34                                          |
      | dataclass   | {"source":"checkout","sample_rate":0.5}    |
      | tuple       | ["paid","beta"]                              |
      | bytes       | "hello"                                       |
      | unsupported | null                                           |

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
