import os
import sys

try:
    from setuptools import setup
except ImportError:
    from distutils.core import setup

# Don't import analytics-python module here, since deps may not be installed
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "posthog"))
from version import VERSION  # noqa: E402

long_description = """
PostHog is developer-friendly, self-hosted product analytics.
posthog-python is the python package.

This package requires Python 3.9 or higher.
"""

# Minimal setup.py for backward compatibility
# Most configuration is now in pyproject.toml
setup(
    name="posthog",
    version=VERSION,
    # Basic fields for backward compatibility
    url="https://github.com/posthog/posthog-python",
    author="Posthog",
    author_email="hey@posthog.com",
    maintainer="PostHog",
    maintainer_email="hey@posthog.com",
    license="MIT License",
    description="Integrate PostHog into any python application.",
    long_description=long_description,
    # This will fallback to pyproject.toml for detailed configuration
)
