# Contributing

This package contains the PostHog Python SDK compliance adapter used with the PostHog SDK Test Harness.

## Running tests

Tests run automatically in CI via GitHub Actions against harness **1.1.1**, for both capture protocols. Both jobs opt into the local-evaluation suite and fail on compliance regressions.

Run adapter protocol tests from the repository root in an activated virtual environment:

```bash
python -m pip install -e . -r sdk_compliance_adapter/requirements.txt pytest pytest-timeout pytest-asyncio
python -m pytest sdk_compliance_adapter/test_adapter.py --timeout=30
```

These tests exercise the real SDK loader/evaluator with controlled transports, including failed/late reloads, local false versus inconclusive, and forced remote evaluation.

### Locally with Docker Compose

Run the full compliance suite from the `sdk_compliance_adapter` directory:

```bash
docker-compose up --build --abort-on-container-exit
```

This will:

1. Build the Python SDK adapter
2. Pull the test harness image
3. Run all compliance tests
4. Show the results

### Manually with Docker

```bash
# Create network
docker network create test-network

# Build and run adapter
docker build -f sdk_compliance_adapter/Dockerfile -t posthog-python-adapter .
docker run -d --name sdk-adapter --network test-network -p 8080:8080 posthog-python-adapter

# Run test harness
docker run --rm \
  --name test-harness \
  --network test-network \
  ghcr.io/posthog/sdk-test-harness:1.1.1 \
  run --adapter-url http://sdk-adapter:8080 --mock-url http://test-harness:8081

# Cleanup
docker stop sdk-adapter && docker rm sdk-adapter
docker network rm test-network
```
