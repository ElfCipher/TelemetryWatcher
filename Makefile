PYTHON ?= .venv/bin/python
PIP ?= .venv/bin/python -m pip

.PHONY: install test lint run build clean

install:
	python3 -m venv .venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements-dev.txt
	$(PIP) install -e .

test:
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m ruff check telemetry tests

run:
	$(PYTHON) -m telemetry

build:
	$(PYTHON) -m build

clean:
	rm -rf build dist .pytest_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
