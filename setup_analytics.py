"""Rewrite pyproject.toml so the build produces the `posthoganalytics` mirror.

Run by `make build_release_analytics` and `make prep_local` after the source has
been copied to `posthoganalytics/`. The original is kept in pyproject.toml.backup.
"""

import shutil

import tomli
import tomli_w

shutil.copy("pyproject.toml", "pyproject.toml.backup")

with open("pyproject.toml", "rb") as f:
    config = tomli.load(f)

config["project"]["name"] = "posthoganalytics"
config["project"]["readme"] = "README_ANALYTICS.md"
config["tool"]["setuptools"]["packages"] = [
    "posthoganalytics" + package.removeprefix("posthog")
    for package in config["tool"]["setuptools"]["packages"]
]
config["tool"]["setuptools"]["exclude-package-data"] = {
    "posthoganalytics": config["tool"]["setuptools"]["exclude-package-data"]["posthog"]
}

with open("pyproject.toml", "wb") as f:
    tomli_w.dump(config, f)
