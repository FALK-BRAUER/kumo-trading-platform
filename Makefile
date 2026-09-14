# kumo-trading-platform — instance-scoped deploy targets.
# Every target takes INSTANCE=<name> and reads instances/<name>/. The checks in bin/ run before any
# container starts; a deploy whose parts disagree is refused, not warned about.

INSTANCE ?= example
INST_DIR := instances/$(INSTANCE)

.PHONY: help check up down logs test

help:
	@echo "make check INSTANCE=<name>   run every bin/check-* against the instance"
	@echo "make up    INSTANCE=<name>   check, then docker compose up"
	@echo "make down  INSTANCE=<name>   docker compose down"
	@echo "make logs  INSTANCE=<name>   follow the stack's logs"
	@echo "make test                    backend pytest + ui vitest"

check:
	@test -d $(INST_DIR) || { echo "no such instance: $(INST_DIR)"; exit 1; }
	bin/check-refs-are-fetchable.sh $(INST_DIR)
	bin/check-secret-vars-agree.sh $(INST_DIR)
	bin/check-compose-honours-instance.sh $(INST_DIR)
	bin/check-public-tree.sh

up: check
	INSTANCE=$(INSTANCE) docker compose --env-file $(INST_DIR)/instance.env -f deploy/compose.paper.yml up -d --build

down:
	INSTANCE=$(INSTANCE) docker compose --env-file $(INST_DIR)/instance.env -f deploy/compose.paper.yml down

logs:
	INSTANCE=$(INSTANCE) docker compose --env-file $(INST_DIR)/instance.env -f deploy/compose.paper.yml logs -f

test:
	cd backend && uv run pytest -q
	cd ui && npm test
