---
pypi/posthog: minor
---

A send with `template_key` now takes `tracking`, which chooses what PostHog tracks for the email. Without it, PostHog counts opens, leaves the links as they are, and captures engagement events. `tracking=True` turns on opens, clicks and engagement events. `tracking=False` turns them all off. A dict such as `{"clicks": True}` or `{"engagement": False}` turns each kind on or off, and a key you leave out keeps its default. The template form doesn't take it.

```python
posthog.emails.send(
    to="user@example.com",
    template_key="newsletter",
    subject="What's new at Acme",
    html=rendered_html,
    tracking={"clicks": True},
)
```

Clicks are off by default because PostHog can't tell whether a link carries a login or password reset token. Turn them on only when the email's links are safe to send through PostHog's click tracking.

`capture_engagement` is deprecated in favor of `tracking={"engagement": False}`. It keeps working, but PostHog refuses a send that sets both.

`tracking` needs PostHog's API support, which is rolling out. Until it reaches your PostHog instance, PostHog refuses a send that sets it. A send without it works as before.
