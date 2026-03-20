"""OpenAI audio transcription (Whisper), tracked by PostHog."""

import os
import sys
from posthog import Posthog
from posthog.ai.openai import OpenAI

posthog = Posthog(
    os.environ["POSTHOG_API_KEY"],
    host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),
)
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], posthog_client=posthog)

# Replace with the path to your audio file
audio_path = os.environ.get("AUDIO_PATH", "audio.mp3")

if not os.path.exists(audio_path):
    print(f"Skipping: audio file not found at '{audio_path}'")
    print("Set AUDIO_PATH to a valid audio file (mp3, wav, m4a, etc.)")
    posthog.shutdown()
    sys.exit(0)

with open(audio_path, "rb") as audio_file:
    transcription = client.audio.transcriptions.create(
        file=audio_file,
        model="whisper-1",
        posthog_distinct_id="example-user",
    )

print(f"Transcription: {transcription.text}")

posthog.shutdown()
