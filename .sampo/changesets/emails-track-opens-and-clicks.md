---
pypi/posthog: minor
---

A send with your own content now takes `track_opens_and_clicks`. It defaults to `True`. With `track_opens_and_clicks=False`, the email goes out without PostHog's open pixel and without links rewritten to count clicks, so PostHog can't count its opens and clicks. It is independent of `capture_engagement`, which decides whether engagement events are captured on the recipient's person. The template form doesn't take it.

```python
posthog.emails.send(
    to="user@example.com",
    template_key="welcome",
    subject="Welcome to Acme",
    html=rendered_html,
    track_opens_and_clicks=False,
)
```

The field needs PostHog's API support, which is rolling out. A send that leaves it out sends the same request as before.
