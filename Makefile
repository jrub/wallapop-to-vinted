# Canonical entry points for development tasks. Targets are intentionally short and
# stable so they can be whitelisted once in .claude/settings.local.json and never
# need a permission prompt again.

PYTHON := .venv/bin/python

.PHONY: install-dev test smoke

install-dev:
	$(PYTHON) -m pip install -r requirements-dev.txt

test:
	$(PYTHON) -m pytest -v

smoke:
	$(PYTHON) upload_vinted.py --help
	$(PYTHON) extract_wallapop.py --help
