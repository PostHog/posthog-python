"""OpenAI audio transcription (Whisper), tracked via OpenTelemetry."""

import os
import sys
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from posthog.ai.otel import PostHogSpanProcessor
from opentelemetry.instrumentation.openai_v2 import OpenAIInstrumentor

resource = Resource(
    attributes={
        SERVICE_NAME: "example-openai-app",
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

client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])

# Replace with the path to your audio file
audio_path = os.environ.get("AUDIO_PATH", "audio.mp3")

if not os.path.exists(audio_path):
    print(f"Skipping: audio file not found at '{audio_path}'")
    print("Set AUDIO_PATH to a valid audio file (mp3, wav, m4a, etc.)")
    sys.exit(0)

with open(audio_path, "rb") as audio_file:
    transcription = client.audio.transcriptions.create(
        file=audio_file,
        model="whisper-1",
    )

print(f"Transcription: {transcription.text}")
