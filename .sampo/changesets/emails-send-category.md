---
pypi/posthog: minor
---

`emails.send` takes an optional `category` on the code form, the one with `template_key`: the key of one of the project's message categories, such as `"newsletter"`. PostHog refuses an unknown key with `category_not_found`, and a marketing email with `cc` or `bcc` with `marketing_copies_unsupported`.
