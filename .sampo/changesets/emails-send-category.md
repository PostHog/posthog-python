---
pypi/posthog: minor
---

`emails.send` takes an optional `category` on the code form, the one with `template_key`. Leave it out, or pass `TRANSACTIONAL_CATEGORY` (`"transactional"`), for transactional email, as before. Any other value is the key of one of the project's marketing message categories: the send honors the recipient's opt-outs, and its body must link `{{ unsubscribe_url }}`. `EmailCategory` types the value. PostHog answers a send it refuses with the error codes `unknown_category`, `unsubscribe_link_required` and `recipient_opted_out`.
