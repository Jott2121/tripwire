.PHONY: demo check lint test scan help

help:
	@echo "make demo   - rebuild both figures, the cost summary and docs/results/demo-1.md from committed data (zero API spend)"
	@echo "make check  - lint + tests (coverage >= 90%) + no-employer-data scan"
	@echo "make lint   - ruff check + format check"
	@echo "make test   - pytest with coverage"
	@echo "make scan   - no-employer-data gate"

demo:
	python scripts/make_results_chart.py
	python scripts/make_phase2_chart.py
	python -m tripwire.demo --render runs/demo-1 --out docs/results/demo-1.md

lint:
	ruff check .
	ruff format --check .

test:
	python -m pytest -q --cov=tripwire --cov-fail-under=90

scan:
	python scripts/scan_blocked_terms.py

check: lint test scan
