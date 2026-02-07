.PHONY: install lint format test clean build-dist publish release

install:
	uv sync --extra dev

lint:
	uv run ruff check src/ tests/

format:
	uv run ruff format src/ tests/

test:
	uv run pytest tests/ -v

clean:
	rm -rf dist/ build/ .mypy_cache/ .pytest_cache/ .ruff_cache/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name '*.egg-info' -exec rm -rf {} + 2>/dev/null || true

# --- Publishing ---

build-dist: clean
	uv run python -m build

publish: build-dist
	uv run twine upload dist/*

release: lint test build-dist
	@echo "Publishing to PyPI..."
	uv run twine upload dist/*
	@echo "Release complete!"
