---
pypi/posthog: minor
---

`posthog.emails.send()` can now send email content your code rendered, such as HTML from Jinja or React Email. Give the email a `template_key` and a `subject`, and pass `html`, `text` or both.

```python
posthog = Posthog("phc_...", secret_key="phs_...")

result = posthog.emails.send(
    to="user@example.com",
    distinct_id="user_123",
    template_key="password-reset",
    subject="Reset your password",
    html=rendered_html,
)

# "delivered", "bounced", ...
status = posthog.emails.get(result.id).status
```

You need a project secret API key, the kind that starts with `phs_`. Create one under **Project secret API keys** in your project settings. Set its **Transactional email** scope to Write to send emails, or to Read for a key that only reads statuses. Pass it to the client as `secret_key`. Set `app_host` to your PostHog app URL when you self-host PostHog or send through a reverse proxy.

PostHog records each email's delivery, opens and clicks on the person with that `distinct_id`. It groups every email with the same `template_key`, so you see their metrics and logs in one place. The SDK retries a request that hits a network error, a rate limit or an outage twice by default. Every send carries an idempotency key, so a retry never sends the email twice. Pass your own `idempotency_key`, such as `welcome:user_123`, and your job runner's retries are safe too.

A send with your own content also takes `from_`, `reply_to` and `capture_engagement=False`. Set `migrated_from` on the client to tell PostHog which email provider you moved from. A failed call raises an `EmailError` with PostHog's `code` and the HTTP `status_code`.

Docs: [Send transactional emails with Python](https://posthog.com/docs/workflows/transactional-emails/python) and the [transactional email overview](https://posthog.com/docs/workflows/transactional-emails).
