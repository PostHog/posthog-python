---
pypi/posthog: minor
---

`emails.send` takes an optional `category` on the code form, the one with `template_key`. Leave it out, or pass `TRANSACTIONAL_CATEGORY` (`"transactional"`), for transactional email with no category, as before. Pass a `MessageCategory` with a `key`, a `type` (`"marketing"` or `"transactional"`), a `name` and an optional `description` to file the email under that category: the first send creates it in the project, and a send that changes its name or description updates it. A marketing send honors the recipient's opt-outs, and its body must link `{{ unsubscribe_url }}`. PostHog refuses a send with the error codes `category_mismatch`, `too_many_categories`, `unsubscribe_link_required` and `recipient_opted_out`.
