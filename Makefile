# iac-driver Makefile

VENV := .venv
VENV_BIN := $(VENV)/bin

.PHONY: help install-deps install-dev test lint

help:
	@echo "iac-driver - Infrastructure orchestration engine"
	@echo ""
	@echo "  make install-deps  - Install required system packages"
	@echo "  make install-dev   - Install development dependencies (pre-commit, linters)"
	@echo "  make test          - Run unit tests"
	@echo "  make lint          - Run pre-commit hooks (pylint, mypy)"
	@echo ""
	@echo "Secrets Management:"
	@echo "  Secrets are managed in the config repository."
	@echo "  See: ../config/ or https://github.com/homestak/config"
	@echo ""
	@echo "  cd ../config && make decrypt"

install-deps:
	@echo "Installing iac-driver dependencies..."
	@apt-get update -qq
	@apt-get install -y -qq python3 python3-yaml python3-requests > /dev/null
	@echo "Done."

install-dev: $(VENV_BIN)/activate
	@echo "Installing development dependencies..."
	$(VENV_BIN)/pip install PyYAML requests pre-commit pylint mypy types-PyYAML types-requests pytest pytest-mock
	$(VENV_BIN)/pre-commit install
	@echo ""
	@echo "Done. Pre-commit hooks installed."
	@echo "Run 'make test' or 'make lint' — no activation needed."

$(VENV_BIN)/activate:
	python3 -m venv $(VENV)

test:
	@echo "Running unit tests..."
	$(VENV_BIN)/python -m pytest tests/ -v

lint:
	@echo "Running pre-commit hooks..."
	$(VENV_BIN)/pre-commit run --all-files
