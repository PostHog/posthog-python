---
pypi/posthog: minor
---

A code send with `posthog.emails.send` takes `cc`, `bcc` and `attachments`: at most 10 files and 40 MB per message after Base64 encoding, about 29 MB of files, with the HTML counted twice when there is no `text`, since PostHog derives the text from it. An attachment is a `filename` and its `content`, as bytes or a Base64 string, with an optional `content_type` and `content_id`. The client checks the limits before any request, uploads each file PostHog doesn't hold yet straight to PostHog's file storage over https, never following a redirect, then sends the email with each file's SHA-256. A refused or redirected upload raises `EmailError` with the code `attachment_upload_failed`, and an unreachable storage `network_error` with `field` naming the attachment. Either way no email is sent.
