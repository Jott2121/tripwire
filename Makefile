.PHONY: demo check lint test scan help

help:
	@echo "make demo   - replay recorded run data into artifacts/ (zero API spend)"
	@echo "make check  - lint + tests (coverage >= 90%) + no-employer-data scan"
	@echo "make lint   - ruff check + format check"
	@echo "make test   - pytest with coverage"
	@echo "make scan   - no-employer-data gate"

demo:
	python scripts/make_results_chart.py

lint:
	ruff check .
	ruff format --check .

test:
	python -m pytest -q --cov=tripwire --cov-fail-under=90

scan:
	python scripts/scan_blocked_terms.py

check: lint test scan
