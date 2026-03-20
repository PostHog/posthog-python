# LangChain + PostHog AI Examples

Track LangChain LLM calls with PostHog.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Fill in your API keys in .env
```

For the OTEL example, also install:

```bash
pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http
```

## Examples

- **callback_handler.py** - PostHog callback handler with tool calling
- **otel.py** - OpenTelemetry instrumentation exporting to PostHog

## Run

```bash
source .env
python callback_handler.py
python otel.py
```
