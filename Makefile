# Common tasks. Python 3.11+ and ffmpeg on the PATH; Chromium via `python -m playwright install chromium`.
.PHONY: install fonts catalog golden serve sheet test test-unit lint format

install:
	pip install -e ".[dev]"

fonts:
	python scripts/fetch_fonts.py

catalog:
	python scripts/build_catalog.py

golden:
	python scripts/build_golden.py out/golden

serve: golden
	python scripts/serve_bundle.py out/golden

sheet: golden
	python scripts/contact_sheet.py out/golden

test:
	pytest

test-unit:
	pytest tests/unit

lint:
	ruff check src scripts tests
	ruff format --check src scripts tests

format:
	ruff check --fix src scripts tests
	ruff format src scripts tests
