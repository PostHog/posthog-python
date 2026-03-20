"""OpenAI audio transcription (Whisper), tracked by PostHog."""

import os
from posthog import Posthog
from posthog.ai.openai import OpenAI

posthog = Posthog(
    os.environ["POSTHOG_API_KEY"],
    host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),
)
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], posthog_client=posthog)

# Replace with the path to your audio file
audio_path = "audio.mp3"

with open(audio_path, "rb") as audio_file:
    transcription = client.audio.transcriptions.create(
        file=audio_file,
        model="whisper-1",
        posthog_distinct_id="example-user",
    )

print(f"Transcription: {transcription.text}")

posthog.shutdown()
