import os
import sys
import tomli
import tomli_w
import shutil

try:
    from setuptools import setup
except ImportError:
    from distutils.core import setup

# Don't import analytics-python module here, since deps may not be installed
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "posthoganalytics"))
from version import VERSION  # noqa: E402


# Copy the original pyproject.toml as backup
shutil.copy("pyproject.toml", "pyproject.toml.backup")

# Read the original pyproject.toml
with open("pyproject.toml", "rb") as f:
    config = tomli.load(f)

# Override specific values
config["project"]["name"] = "posthoganalytics"
config["tool"]["setuptools"]["dynamic"]["version"] = {
    "attr": "posthoganalytics.version.VERSION"
}

# Rename packages from posthog.* to posthoganalytics.*
if "packages" in config["tool"]["setuptools"]:
    new_packages = []
    for package in config["tool"]["setuptools"]["packages"]:
        if package == "posthog":
            new_packages.append("posthoganalytics")
        elif package.startswith("posthog."):
            new_packages.append(package.replace("posthog.", "posthoganalytics.", 1))
        else:
            new_packages.append(package)
    config["tool"]["setuptools"]["packages"] = new_packages

# Overwrite the original pyproject.toml
with open("pyproject.toml", "wb") as f:
    tomli_w.dump(config, f)

long_description = """
PostHog is developer-friendly, self-hosted product analytics.
posthog-python is the python package.

This package requires Python 3.9 or higher.
"""

# Minimal setup.py for backward compatibility
# Most configuration is now in pyproject.toml
setup(
    name="posthoganalytics",
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
