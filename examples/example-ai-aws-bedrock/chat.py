"""AWS Bedrock chat with OpenTelemetry instrumentation, tracked by PostHog."""

import os
import boto3
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.botocore import BotocoreInstrumentor

resource = Resource(attributes={SERVICE_NAME: "example-bedrock-app"})

exporter = OTLPSpanExporter(
    endpoint=f"{os.environ.get('POSTHOG_HOST', 'https://us.i.posthog.com')}/i/v0/ai/otel",
    headers={"Authorization": f"Bearer {os.environ['POSTHOG_API_KEY']}"},
)

provider = TracerProvider(resource=resource)
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)

BotocoreInstrumentor().instrument()

client = boto3.client(
    "bedrock-runtime",
    region_name=os.environ.get("AWS_REGION", "us-east-1"),
)

response = client.converse(
    modelId="openai.gpt-oss-20b-1:0",
    messages=[
        {
            "role": "user",
            "content": [{"text": "Tell me a fun fact about hedgehogs."}],
        }
    ],
)

for block in response["output"]["message"]["content"]:
    if "text" in block:
        print(block["text"])
        break
