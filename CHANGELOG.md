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
