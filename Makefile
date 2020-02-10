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
	rm -rf posthog-analytics
	mkdir posthog-analytics
	cp -r posthog/* posthog-analytics/
	python setup_analytics.py sdist bdist_wheel
	twine upload dist/*
	rm -rf posthog-analytics

e2e_test:
	.buildscripts/e2e.sh

.PHONY: test release e2e_test
