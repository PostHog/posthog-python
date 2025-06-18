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
	cd ../posthog-python-local && sed -i.bak 's/from version import VERSION/from posthoganalytics.version import VERSION/' setup_analytics.py
	cd ../posthog-python-local && rm setup_analytics.py.bak
	cd ../posthog-python-local && sed -i.bak 's/"posthog"/"posthoganalytics"/' setup.py
	cd ../posthog-python-local && rm setup.py.bak
	cd ../posthog-python-local && python -c "import setup_analytics" 2>/dev/null || true
	@echo "Local copy created at ../posthog-python-local"
	@echo "Install with: pip install -e ../posthog-python-local"

.PHONY: test lint release e2e_test prep_local
