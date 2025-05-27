import os
import sys

try:
    from setuptools import setup
except ImportError:
    from distutils.core import setup

# Don't import module here, since deps may not be installed
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "posthoganalytics"))
from version import VERSION

long_description = """
PostHog is developer-friendly, self-hosted product analytics. posthog-python is the python package.

This package requires Python 3.9 or higher.
"""

install_requires = [
    "requests>=2.7,<3.0",
    "six>=1.5",
    "python-dateutil>=2.2",
    "backoff>=1.10.0",
    "distro>=1.5.0",  # Required for Linux OS detection in Python 3.9+
]

tests_require = ["mock>=2.0.0"]

setup(
    name="posthoganalytics",
    version=VERSION,
    url="https://github.com/posthog/posthog-python",
    author="Posthog",
    author_email="hey@posthog.com",
    maintainer="PostHog",
    maintainer_email="hey@posthog.com",
    test_suite="posthoganalytics.test.all",
    packages=[
        "posthoganalytics",
        "posthoganalytics.ai",
        "posthoganalytics.ai.langchain",
        "posthoganalytics.ai.openai",
        "posthoganalytics.ai.anthropic",
        "posthoganalytics.ai.gemini",
        "posthoganalytics.test",
        "posthoganalytics.sentry",
        "posthoganalytics.exception_integrations",
    ],
    license="MIT License",
    install_requires=install_requires,
    tests_require=tests_require,
    extras_require={
        "sentry": ["sentry-sdk", "django"],
    },
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
