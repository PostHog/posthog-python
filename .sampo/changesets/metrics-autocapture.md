---
pypi/posthog: minor
---

Add `metrics={"autocapture": True}` (alpha): HTTP, database and runtime metrics from the official OpenTelemetry instrumentations, with no instrumentation code. Install `posthog[metrics]` and run `opentelemetry-bootstrap -a install` to add the instrumentors for your libraries. Create the client before your web app (Django: in `AppConfig.ready()`): apps created earlier are not measured. For Flask, use module lookup as below, or import the `Flask` class only after the client starts; an earlier `from flask import Flask` alias is not instrumented. Already-served Flask apps are not re-instrumented after shutdown; create a new app for a replacement client. Autocapture does not start when the app already set up OpenTelemetry; export those metrics to PostHog instead. Raw URLs and paths, client addresses, user agents and captured headers are never recorded. `metrics.before_send` also applies to autocaptured measurements: it can change their value and attributes, or drop them.

```python
import flask
from posthog import Posthog

client = Posthog("phc_...", metrics={"autocapture": True})
app = flask.Flask(__name__)
```
