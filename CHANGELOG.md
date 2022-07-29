## 2.0.0 - 2022-07-??

Breaking changes:

1. The minimum version requirement for PostHog servers is now 1.38. If you're using PostHog Cloud, you satisfy this requirement automatically.
1. Defaults apply only when there's an error fetching feature flag results. Earlier, if the default was set to `True`, even if a flag resolved to `False`, the default would override this.

This change introduces local evaluation of feature flags, which allows you to compute flags much quicker locally, with no requests going to your PostHog instance server, as long as you know the user properties on which the feature flag depends.


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
