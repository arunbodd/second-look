.PHONY: install run test record docker
install:        ## create .venv and install dependencies
	python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
run:            ## http on 8000 and https on 8443
	. .venv/bin/activate && python serve.py
test:
	. .venv/bin/activate && pytest -q
record:         ## refresh data/recorded_review.json (needs API keys in .env)
	. .venv/bin/activate && python -m app.record
docker:
	docker compose up --build
