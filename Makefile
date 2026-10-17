IMAGE ?= linkwatch
PYTHON ?= python3.12

.PHONY: setup test test-nose docker-build docker-test

setup:
	$(PYTHON) -m venv .venv
	.venv/bin/pip install -r requirements.txt -r requirements-dev.txt

test:
	python -m pytest

test-nose:
	nosetests tests/

docker-build:
	docker build -t $(IMAGE) .

docker-test:
	docker run --rm $(IMAGE) pytest
