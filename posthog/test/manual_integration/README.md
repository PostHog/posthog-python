# Manual Integration Tests

This directory contains integration tests that require external dependencies and are intended to be run manually during local development. **These tests are NOT run automatically in CI.**

## Difference from `posthog/test/integrations/`

- **`posthog/test/integrations/`**: Framework integration tests (Django middleware, etc.) that use mocks and are safe to run in CI
- **`posthog/test/manual_integration/`**: True integration tests that require live external services and should only be run manually

## Flag Dependencies Integration Test

The `test_flag_dependencies.py` file contains integration tests for feature flag dependencies functionality. **This test is fully self-contained** - it creates its own test flags and cleans them up automatically.

### Prerequisites

1. **Running PostHog Instance**: You need a running PostHog instance (default: `http://localhost:8000`)
2. **API Keys**: Valid project and personal API keys with permissions to create and delete feature flags
3. **No manual setup required**: The test creates and deletes its own flags automatically

**Note**: The test will fail gracefully if PostHog is not running or if the API keys are invalid, providing clear error messages about connectivity issues.

### Configuration

Set environment variables to customize the test configuration:

```bash
export POSTHOG_HOST="http://localhost:8000"
export POSTHOG_API_KEY="your-project-api-key"
export POSTHOG_PERSONAL_API_KEY="your-personal-api-key"
export POSTHOG_TEST_EMAIL="phil.h@posthog.com"
export POSTHOG_TEST_EMAIL_DISABLED="other@example.com"

# Optional: Enable debug mode for verbose output
export POSTHOG_DEBUG=true
```

### Running the Tests

#### Using the bin script (Recommended)
```bash
# Run with minimal output
bin/run_integration_tests

# Run with debug output (verbose)
bin/run_integration_tests --debug
```

#### Using pytest directly
```bash
# Run all manual integration tests
pytest posthog/test/manual_integration/ -v

# Run specific test
pytest posthog/test/manual_integration/test_flag_dependencies.py -v
```

#### As a Python Module
```bash
python -m posthog.test.manual_integration.test_flag_dependencies
```

#### Direct execution
```bash
python posthog/test/manual_integration/test_flag_dependencies.py
```

### Test Coverage

The integration tests verify:

1. **Flag Dependencies for Enabled Users**: Users with matching criteria have both base and dependent flags enabled
2. **Flag Dependencies for Disabled Users**: Users without matching criteria have both flags disabled
3. **Dependency Graph Building**: The client properly builds dependency graphs from flag configurations
4. **Evaluation Consistency**: Flag evaluation results are consistent across multiple calls
5. **API Flag Management**: Creating and deleting flags via the PostHog API works correctly

### How It Works

The test automatically:

1. **Creates unique test flags**: Generates unique flag keys for each test run using UUIDs
2. **Sets up dependencies**: Creates a base flag and a dependent flag that depends on the base flag
3. **Tests flag evaluation**: Verifies that the dependency logic works correctly
4. **Cleans up**: Automatically deletes the created flags after tests complete

This makes the test completely self-contained and safe to run multiple times without conflicts.

### Expected Output

When successful, you should see output similar to:

```
🚀 Setting up integration test
📍 PostHog Host: http://localhost:8000
🔑 API Key: phc_zKpz2SQ32LaNyftKL8...
🔑 Personal API Key: phx_DhDz...
📧 Test Email (enabled): phil.h@posthog.com
📧 Test Email (disabled): other@example.com
🏷️  Base Flag Key: test-base-flag-a1b2c3d4
🏷️  Dependent Flag Key: test-dependent-flag-a1b2c3d4

📋 Creating test flags...
✅ Created flag: test-base-flag-a1b2c3d4 (ID: 123)
✅ Created flag: test-dependent-flag-a1b2c3d4 (ID: 124)
✅ Created dependency: test-dependent-flag-a1b2c3d4 depends on test-base-flag-a1b2c3d4
⏳ Waiting for flags to be available...
✅ Feature flags loaded successfully
✅ phil.h@posthog.com: test-base-flag-a1b2c3d4=True, test-dependent-flag-a1b2c3d4=True
✅ other@example.com: test-base-flag-a1b2c3d4=False, test-dependent-flag-a1b2c3d4=False
✅ Dependency graph is properly built
✅ Flag evaluation is consistent: True
✅ API flag creation and cleanup working correctly

🧹 Cleaning up test flags...
✅ Deleted flag ID: 124
✅ Deleted flag ID: 123
✅ Cleanup completed

============================================================
🎉 All integration tests PASSED!
✅ Flag dependencies are working correctly
============================================================
```

### Troubleshooting

If tests fail, check:

1. **PostHog Instance**: Ensure your PostHog instance is running and accessible
   - For local development: `http://localhost:8000`
   - For PostHog Cloud: `https://app.posthog.com` or `https://eu.posthog.com`
2. **API Keys**: Verify your API keys are correct and have proper permissions to create/delete flags
3. **Personal API Key**: Ensure your personal API key has admin permissions for feature flag management
4. **Network**: Check network connectivity to the PostHog instance
5. **Permissions**: Verify that your API keys have the necessary permissions to create and delete feature flags

**Common Error Messages:**
- `Cannot connect to PostHog instance`: PostHog server is not running or not accessible
- `HTTP 401`: Invalid API keys or insufficient permissions
- `HTTP 404`: Incorrect PostHog host URL
- `Connection timeout`: Network connectivity issues

### Adding New Manual Integration Tests

To add new manual integration tests:

1. Create a new test file in this directory
2. Follow the existing patterns for configuration and setup
3. Use environment variables for configuration
4. Include proper documentation and error handling
5. Add test description to this README
6. Ensure the test is self-contained and includes cleanup