import os
import sys

try:
    from setuptools import setup
except ImportError:
    from distutils.core import setup

# Don't import analytics-python module here, since deps may not be installed
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "posthog"))
from version import VERSION

long_description = """
PostHog is developer-friendly, self-hosted product analytics. posthog-python is the python package.
"""

install_requires = [
    "requests>=2.7,<3.0",
    "six>=1.5",
    "monotonic>=1.5",
    "backoff>=1.10.0",
    "python-dateutil>2.1",
    "distro>=1.5.0",  # Required for Linux OS detection in Python 3.9+
]

extras_require = {
    "dev": [
        "black",
        "django-stubs",
        "isort",
        "flake8",
        "flake8-print",
        "lxml",
        "mypy",
        "mypy-baseline",
        "types-mock",
        "types-python-dateutil",
        "types-requests",
        "types-setuptools",
        "types-six",
        "pre-commit",
        "pydantic",
    ],
    "test": [
        "mock>=2.0.0",
        "freezegun==1.5.1",
        "pylint",
        "flake8",
        "coverage",
        "pytest",
        "pytest-timeout",
        "pytest-asyncio",
        "django",
        "openai",
        "anthropic",
        "langgraph",
        "langchain-community>=0.2.0",
        "langchain-openai>=0.2.0",
        "langchain-anthropic>=0.2.0",
        "pydantic",
        "parameterized>=0.8.1",
    ],
    "sentry": ["sentry-sdk", "django"],
    "langchain": ["langchain>=0.2.0"],
}

setup(
    name="posthog",
    version=VERSION,
    url="https://github.com/posthog/posthog-python",
    author="Posthog",
    author_email="hey@posthog.com",
    maintainer="PostHog",
    maintainer_email="hey@posthog.com",
    test_suite="posthog.test.all",
    packages=[
        "posthog",
        "posthog.ai",
        "posthog.ai.langchain",
        "posthog.ai.openai",
        "posthog.ai.anthropic",
        "posthog.test",
        "posthog.sentry",
        "posthog.exception_integrations",
    ],
    license="MIT License",
    install_requires=install_requires,
    extras_require=extras_require,
    description="Integrate PostHog into any python application.",
    long_description=long_description,
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
    ],
)
