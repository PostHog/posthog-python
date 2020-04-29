# PostHog Python

Official PostHog Python library to capture and send events to any PostHog instance (including PostHog.com).

This library uses an internal queue to make calls non-blocking and fast. It also batches requests and flushes asynchronously, making it perfect to use in any part of your web app or other server side application that needs performance.

See [PostHog docs](https://docs.posthog.com) for all documentation, including our [Python docs](https://docs.posthog.com/#/integrations/python-integration).
