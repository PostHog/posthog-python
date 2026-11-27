lint:
	uvx ruff format

test:
	coverage run -m pytest
	coverage report

public_api_snapshot:
	python .github/scripts/check_public_api.py --write

public_api_check:
	python .github/scripts/check_public_api.py

build_release:
	rm -rf dist/*
	uv build --out-dir dist

# Builds the `posthoganalytics` PyPI package, which is a mirror of `posthog`
# published under a different name for internal use by posthog/posthog.
#
# The process works in three phases:
#   1. posthog -> posthoganalytics: Copy the source, rewrite all imports,
#      remove the original posthog/ dir, and build the dist.
#   2. posthoganalytics -> posthog: Reverse the import rewrites, copy
#      everything back into posthog/, and clean up.
#   3. Restore pyproject.toml from backup (setup_analytics.py modifies it).
#
# This ensures the working tree is left in the same state it started in.
#
# NOTE: This target clears dist/ before building. In the release workflow,
# `build_release` (posthog) must be published BEFORE running this target,
# otherwise the posthog dist artifacts will be lost.
build_release_analytics:
	rm -rf dist
	rm -rf build
	rm -rf posthoganalytics
	mkdir posthoganalytics
	cp -r posthog/* posthoganalytics/
	find ./posthoganalytics -type f -name "*.py" -exec sed -i.bak -e 's/from posthog /from posthoganalytics /g' {} \;
	find ./posthoganalytics -type f -name "*.py" -exec sed -i.bak -e 's/from posthog\./from posthoganalytics\./g' {} \;
	find ./posthoganalytics -name "*.bak" -delete
	rm -rf posthog
	python setup_analytics.py
	uv build --out-dir dist
	mkdir posthog
	find ./posthoganalytics -type f -name "*.py" -exec sed -i.bak -e 's/from posthoganalytics /from posthog /g' {} \;
	find ./posthoganalytics -type f -name "*.py" -exec sed -i.bak -e 's/from posthoganalytics\./from posthog\./g' {} \;
	find ./posthoganalytics -name "*.bak" -delete
	cp -r posthoganalytics/* posthog/
	rm -rf posthoganalytics
	rm -f pyproject.toml
	cp pyproject.toml.backup pyproject.toml
	rm -f pyproject.toml.backup

e2e_test:
	.buildscripts/e2e.sh

prep_local:
	rm -rf ../posthog-python-local
	mkdir ../posthog-python-local
	cp -r . ../posthog-python-local/
	cd ../posthog-python-local && rm -rf dist build posthoganalytics .git
	cd ../posthog-python-local && mkdir posthoganalytics
	cd ../posthog-python-local && cp -r posthog/* posthoganalytics/
	cd ../posthog-python-local && find ./posthoganalytics -type f -name "*.py" -exec sed -i.bak -e 's/from posthog /from posthoganalytics /g' {} \;
	cd ../posthog-python-local && find ./posthoganalytics -type f -name "*.py" -exec sed -i.bak -e 's/from posthog\./from posthoganalytics\./g' {} \;
	cd ../posthog-python-local && find ./posthoganalytics -name "*.bak" -delete
	cd ../posthog-python-local && rm -rf posthog
	cd ../posthog-python-local && python setup_analytics.py && rm pyproject.toml.backup
	@echo "Local copy created at ../posthog-python-local"
	@echo "Install with: pip install -e ../posthog-python-local"

.PHONY: test lint public_api_snapshot public_api_check build_release build_release_analytics e2e_test prep_local
