# Canonical entry points for development tasks. Targets are intentionally short and
# stable so they can be whitelisted once in .claude/settings.local.json and never
# need a permission prompt again.

PYTHON := .venv/bin/python

.PHONY: install-dev test test-fast test-browser smoke

install-dev:
	$(PYTHON) -m pip install -r requirements-dev.txt

test:
	$(PYTHON) -m pytest -v

test-fast:
	$(PYTHON) -m pytest -v -m "not playwright"

test-browser:
	$(PYTHON) -m pytest -v -m playwright

smoke:
	$(PYTHON) upload_vinted.py --help
	$(PYTHON) extract_wallapop.py --help
