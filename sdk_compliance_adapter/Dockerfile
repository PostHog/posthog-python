FROM python:3.12-slim

WORKDIR /app

# Copy the SDK source code
COPY . /app/sdk

# Install the SDK from source
RUN cd /app/sdk && pip install --no-cache-dir -e .

# Install adapter dependencies
RUN pip install --no-cache-dir flask python-dateutil

# Copy adapter code
COPY sdk_compliance_adapter/adapter.py /app/adapter.py

# Expose port 8080
EXPOSE 8080

# Set environment variables (will be overridden by docker run)
ENV POSTHOG_HOST=http://localhost:8080
ENV POSTHOG_API_KEY=test_key
ENV PORT=8080

# Run the adapter
CMD ["python", "/app/adapter.py"]
