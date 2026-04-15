"""Cloudflare AI Gateway chat completions via OpenAI-compatible API, tracked by PostHog via OpenTelemetry."""

import os
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from posthog.ai.otel import PostHogSpanProcessor
from opentelemetry.instrumentation.openai_v2 import OpenAIInstrumentor

resource = Resource(
    attributes={
        SERVICE_NAME: "example-cloudflare-ai-gateway-app",
        "posthog.distinct_id": "example-user",
        "foo": "bar",
        "conversation_id": "abc-123",
    }
)
provider = TracerProvider(resource=resource)
provider.add_span_processor(
    PostHogSpanProcessor(
        api_key=os.environ["POSTHOG_API_KEY"],
        host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),
    )
)
trace.set_tracer_provider(provider)

OpenAIInstrumentor().instrument()

import openai  # noqa: E402

account_id = os.environ["CLOUDFLARE_ACCOUNT_ID"]
gateway_id = os.environ["CLOUDFLARE_GATEWAY_ID"]

client = openai.OpenAI(
    base_url=f"https://gateway.ai.cloudflare.com/v1/{account_id}/{gateway_id}/compat",
    api_key=os.environ["OPENAI_API_KEY"],
)

response = client.chat.completions.create(
    model="openai/gpt-5-mini",
    max_completion_tokens=1024,
    messages=[
        {"role": "user", "content": "Tell me a fun fact about hedgehogs."},
    ],
)

print(response.choices[0].message.content)
