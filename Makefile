test:
	pylint --rcfile=.pylintrc --reports=y --exit-zero analytics | tee pylint.out
	flake8 --max-complexity=10 --statistics analytics > flake8.out || true
	coverage run --branch --include=analytics/\* --omit=*/test* setup.py test

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
	find ./posthoganalytics -type f -exec sed -i '' -e 's/from posthog\./from posthoganalytics\./g' {} \;
	rm -rf posthog
	python setup_analytics.py sdist bdist_wheel
	twine upload dist/*
	mkdir posthog
	find ./posthoganalytics -type f -exec sed -i '' -e 's/from posthoganalytics\./from posthog\./g' {} \;
	cp -r posthoganalytics/* posthog/
	rm -rf posthoganalytics

e2e_test:
	.buildscripts/e2e.sh

.PHONY: test release e2e_test
