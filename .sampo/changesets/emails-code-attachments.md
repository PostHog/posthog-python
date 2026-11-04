---
pypi/posthog: minor
---

A code send with `posthog.emails.send` takes `cc`, `bcc` and `attachments`. An attachment is a `filename` and its `content`, as bytes or a Base64 string, with an optional `content_type` and `content_id`. An attachment the client can't send raises `EmailError` with the code `invalid_attachment` before any request.
