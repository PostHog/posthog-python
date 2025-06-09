# PostHog Python

<p align="center">
  <img alt="posthoglogo" src="https://user-images.githubusercontent.com/65415371/205059737-c8a4f836-4889-4654-902e-f302b187b6a0.png">
</p>
<p align="center">
   <a href="https://pypi.org/project/posthog/"><img alt="pypi installs" src="https://img.shields.io/pypi/v/posthog"/></a>
   <img alt="GitHub contributors" src="https://img.shields.io/github/contributors/posthog/posthog-python">
  <img alt="GitHub commit activity" src="https://img.shields.io/github/commit-activity/m/posthog/posthog-python"/>
  <img alt="GitHub closed issues" src="https://img.shields.io/github/issues-closed/posthog/posthog-python"/>
</p>

Please see the [Python integration docs](https://posthog.com/docs/integrations/python-integration) for details.

## Development

### Testing Locally

We recommend using [uv](https://docs.astral.sh/uv/). It's super fast.

1. Run `uv venv env` (creates virtual environment called "env")
    * or `python3 -m venv env`
2. Run `source env/bin/activate` (activates the virtual environment)
3. Run `uv sync --extra dev --extra test` (installs the package in develop mode, along with test dependencies)
    * or `pip install -e ".[dev,test]"`
4. you have to run `pre-commit install` to have auto linting pre commit
5. Run `make test`
  1. To run a specific test do `pytest -k test_no_api_key`

## PostHog recommends `uv` so...

```bash
uv python install 3.9.19
uv python pin 3.9.19
uv venv env
source env/bin/activate
uv sync --extra dev --extra test
pre-commit install
make test
```

### Running Locally

Assuming you have a [local version of PostHog](https://posthog.com/docs/developing-locally) running, you can run `python3 example.py` to see the library in action.

### Running the Django Sentry Integration Locally

There's a sample Django project included, called `sentry_django_example`, which explains how to use PostHog with Sentry.

There's 2 places of importance (Changes required are all marked with TODO in the sample project directory)

1. Settings.py
    1. Input your Sentry DSN
    2. Input your Sentry Org and ProjectID details into `PosthogIntegration()`
    3. Add `POSTHOG_DJANGO` to settings.py. This allows the `PosthogDistinctIdMiddleware` to get the distinct_ids

2. urls.py
    1. This includes the `sentry-debug/` endpoint, which generates an exception

To run things: `make django_example`. This installs the posthog-python library with the sentry-sdk add-on, and then runs the django app.
Also start the PostHog app locally.
Then navigate to `http://127.0.0.1:8080/sentry-debug/` and you should get an event in both Sentry and PostHog, with links to each other.

### Releasing Versions

Updated are released using GitHub Actions: after bumping `version.py` in `master` and adding to `CHANGELOG.md`, go to [our release workflow's page](https://github.com/PostHog/posthog-python/actions/workflows/release.yaml) and dispatch it manually, using workflow from `master`.