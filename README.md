# PostHog Python

Please see the main [PostHog docs](https://posthog.com/docs).

Specifically, the [Python integration](https://posthog.com/docs/integrations/python-integration) details.

## Questions?

### [Join our Slack community.](https://join.slack.com/t/posthogusers/shared_invite/enQtOTY0MzU5NjAwMDY3LTc2MWQ0OTZlNjhkODk3ZDI3NDVjMDE1YjgxY2I4ZjI4MzJhZmVmNjJkN2NmMGJmMzc2N2U3Yjc3ZjI5NGFlZDQ)

# Running & Testing Locally

1. Run `python3 -m venv env` (creates virtual environment called "env")
2. Run `source env/bin/activate` (activates the virtual environment)
3. Run `python3 -m pip install -e ".[test]"` (installs the package in develop mode, along with test dependencies)
4. Run `make test`

To run tests specific to a file, use 