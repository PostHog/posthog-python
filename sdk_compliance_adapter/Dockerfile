FROM python:3.12-slim

WORKDIR /app

# Copy the SDK source code
COPY posthog/ /app/sdk/posthog/
COPY setup.py pyproject.toml README.md LICENSE /app/sdk/

# Install the SDK from source
RUN cd /app/sdk && pip install --no-cache-dir -e .

# Install adapter dependencies
RUN pip install --no-cache-dir flask python-dateutil

# Copy adapter code
COPY sdk_compliance_adapter/adapter.py /app/adapter.py

# Expose port 8080
EXPOSE 8080

# Run the adapter
CMD ["python", "/app/adapter.py"]
