lint:
	uvx ruff format

test:
	coverage run -m pytest
	coverage report

release:
	rm -rf dist/*
	python setup.py sdist bdist_wheel
	twine upload dist/*

release_analytics:
	rm -rf dist
	rm -rf build
	rm -rf posthoganalytics
	mkdir posthoganalytics
	cp -r posthog/* posthoganalytics/
	find ./posthoganalytics -type f -name "*.py" -exec sed -i.bak -e 's/from posthog /from posthoganalytics /g' {} \;
	find ./posthoganalytics -type f -name "*.py" -exec sed -i.bak -e 's/from posthog\./from posthoganalytics\./g' {} \;
	find ./posthoganalytics -name "*.bak" -delete
	rm -rf posthog
	python setup_analytics.py sdist bdist_wheel
	twine upload dist/*
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

.PHONY: test lint release e2e_test
