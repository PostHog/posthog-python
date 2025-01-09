import os
import uuid

import posthog
from posthog.ai import OpenAI

# Example credentials - replace these with your own or use environment variables
posthog.project_api_key = os.getenv("POSTHOG_PROJECT_API_KEY", "your-project-api-key")
posthog.personal_api_key = os.getenv("POSTHOG_PERSONAL_API_KEY", "your-personal-api-key")
posthog.host = os.getenv("POSTHOG_HOST", "http://localhost:8000")  # Or https://app.posthog.com
posthog.debug = True

openai_client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY", "your-openai-api-key"),
    posthog_client=posthog,
)

def main():
    trace_id = str(uuid.uuid4())
    print("Trace ID:", trace_id)

    try:
        print("Calling OpenAI")
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a complex problem solver."},
                {"role": "user", "content": "Explain quantum computing in simple terms."},
            ],
            max_tokens=100,
            temperature=0.7,
            posthog_distinct_id="user_12345",
            posthog_trace_id=trace_id,
            posthog_properties={"example_key": "example_value"},
        )
        print("RESPONSE:", response)

        if response and response.choices:
            print("OpenAI response:", response.choices[0].message.content)
        else:
            print("No response or unexpected format returned.")
    except Exception as e:
        print("Error during OpenAI call:", str(e))

if __name__ == "__main__":
    main()