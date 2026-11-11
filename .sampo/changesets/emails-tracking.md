---
pypi/posthog: minor
---

A send with `template_key` now takes `tracking` and `sensitive`. The template form doesn't take either one.

`tracking` chooses what PostHog tracks for the email. `tracking=True` turns on opens, clicks and engagement events. `tracking=False` turns them all off. A dict such as `{"clicks": True}` or `{"engagement": False}` turns each kind on or off, and a key you leave out keeps its default. Without it, PostHog counts opens, captures engagement events, and counts clicks only when the email isn't sensitive.

`sensitive` marks an email that carries login links, reset tokens or codes. A sensitive email keeps its links as they are unless `tracking` turns clicks on, leaves its subject off engagement events, and keeps its content out of PostHog's logs. Without it, a send whose `template_key` matches a template saved in PostHog follows that template's setting, and any other send is sensitive. A send that sets neither option works exactly as before.

```python
posthog.emails.send(
    to="user@example.com",
    template_key="newsletter",
    subject="What's new at Acme",
    html=rendered_html,
    sensitive=False,
)
```

`capture_engagement` is deprecated in favor of `tracking={"engagement": False}`. It keeps working, but PostHog refuses a send that sets both.

`tracking` and `sensitive` need PostHog's API support, which is rolling out. Until it reaches your PostHog instance, PostHog refuses a send that sets either one.
