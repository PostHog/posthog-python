---
pypi/posthog: patch
---

Honor `GEMINI_API_KEY` when initializing the synchronous and asynchronous Gemini clients, after `GOOGLE_API_KEY` and before the legacy `API_KEY` fallback.
