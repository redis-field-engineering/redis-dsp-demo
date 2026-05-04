PYTHON ?= python3
UVICORN ?= uvicorn

.PHONY: install format lint test up down logs generate-data load-data run loadtest evaluate benchmark notebooks

install:
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -e ".[dev]"

format:
	$(PYTHON) -m ruff format .

lint:
	$(PYTHON) -m ruff check .

test:
	$(PYTHON) -m pytest

up:
	docker compose up --build -d

down:
	docker compose down --remove-orphans

logs:
	docker compose logs -f app redis otel-collector prometheus grafana

generate-data:
	$(PYTHON) -m data.synthetic --output data/generated/synthetic

load-data:
	$(PYTHON) -m data.load_redis --dataset-dir data/generated/synthetic

run:
	$(UVICORN) app.main:app --host 0.0.0.0 --port 8000

loadtest:
	$(PYTHON) -m loadtest.run --base-url http://localhost:8000

evaluate:
	$(PYTHON) -m experiments.evaluate --dataset-dir data/generated/synthetic

benchmark:
	$(PYTHON) -m experiments.benchmark --base-url http://localhost:8000 --dataset-dir data/generated/synthetic

notebooks:
	$(PYTHON) -m jupyter lab notebooks
