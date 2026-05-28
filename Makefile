COMPOSE = docker compose -f docker-compose.yml
SIMULATOR_REPO_URL ?= https://github.com/Digital-Twins-RSBR/iot_simulator.git
CLIENT_REPO_URL ?= https://github.com/Digital-Twins-RSBR/middts-client.git
PUBLIC_GIT_CLONE = env GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 git clone
SIMULATOR_CONTEXT ?= $(shell if [ -d ./iot_simulator ]; then echo ./iot_simulator; elif [ -d ../iot_simulator ]; then echo ../iot_simulator; else echo ""; fi)
CLIENT_CONTEXT ?= $(shell if [ -d ./middts-client ]; then echo ./middts-client; elif [ -d ../middts-client ]; then echo ../middts-client; else echo ""; fi)
COMPOSE_WITH_CONTEXT = SIMULATOR_CONTEXT="$(SIMULATOR_CONTEXT)" CLIENT_CONTEXT="$(CLIENT_CONTEXT)" $(COMPOSE)

PHONY: help deps build-with-deps ensure-simulator-context ensure-client-context ensure-build-contexts build build-refresh build-nocache up down restart logs migrate collectstatic shell sim-up sim-down client-up client-down client-build update db-backup deploy clean fullclean prune-safe prune-all space-report seed-house discover-gateways discover-devices seed-house-devices

help:
	@echo "Usage: make <target>"
	@echo "Main flow: make build && make up"
	@echo "Bootstrap: deps build-with-deps"
	@echo "Targets: deps build-with-deps build build-refresh build-nocache up down restart logs migrate collectstatic shell sim-up sim-down client-build client-up client-down update db-backup deploy clean fullclean prune-safe prune-all space-report seed-house discover-devices seed-house-devices"
	@if [ -n "$(SIMULATOR_CONTEXT)" ]; then echo "Simulator context auto-detected: $(SIMULATOR_CONTEXT)"; else echo "Simulator context not found (expected ./iot_simulator or ../iot_simulator)"; fi
	@if [ -n "$(CLIENT_CONTEXT)" ]; then echo "Client context auto-detected: $(CLIENT_CONTEXT)"; else echo "Client context not found (expected ./middts-client or ../middts-client)"; fi

deps:
	@if [ -f .gitmodules ]; then \
		echo "[deps] .gitmodules detected: syncing/updating submodules..."; \
		git submodule sync --recursive; \
		git submodule update --init --recursive; \
	else \
		echo "[deps] No .gitmodules found. Bootstrapping dependency repositories with git clone..."; \
		if [ ! -d ./iot_simulator ]; then \
			echo "[deps] Cloning iot_simulator into ./iot_simulator"; \
			$(PUBLIC_GIT_CLONE) "$(SIMULATOR_REPO_URL)" ./iot_simulator || { \
				echo "[deps][ERROR] Failed to clone iot_simulator from $(SIMULATOR_REPO_URL)"; \
				echo "[deps][ERROR] Check network access or clone manually into ./iot_simulator"; \
				exit 1; \
			}; \
		else \
			echo "[deps] iot_simulator already present at ./iot_simulator"; \
		fi; \
		if [ ! -d ./middts-client ]; then \
			echo "[deps] Cloning middts-client into ./middts-client"; \
			$(PUBLIC_GIT_CLONE) "$(CLIENT_REPO_URL)" ./middts-client || { \
				echo "[deps][ERROR] Failed to clone middts-client from $(CLIENT_REPO_URL)"; \
				echo "[deps][ERROR] This can happen when the environment injects Git credentials/config automatically."; \
				echo "[deps][ERROR] Try cloning manually or set CLIENT_CONTEXT to an existing local path."; \
				exit 1; \
			}; \
		else \
			echo "[deps] middts-client already present at ./middts-client"; \
		fi; \
	fi

build-with-deps: deps build
	@echo "build-with-deps finished"

ensure-simulator-context:
	@if [ -z "$(SIMULATOR_CONTEXT)" ] || [ ! -d "$(SIMULATOR_CONTEXT)" ]; then \
		echo "[ERROR] Simulator context not found."; \
		echo "Expected one of:"; \
		echo "  - ./iot_simulator"; \
		echo "  - ../iot_simulator"; \
		echo "Or run with explicit path:"; \
		echo "  make SIMULATOR_CONTEXT=/absolute/or/relative/path build"; \
		exit 1; \
	fi

ensure-client-context:
	@if [ -z "$(CLIENT_CONTEXT)" ] || [ ! -d "$(CLIENT_CONTEXT)" ]; then \
		echo "[ERROR] Client context not found."; \
		echo "Expected one of:"; \
		echo "  - ./middts-client"; \
		echo "  - ../middts-client"; \
		echo "Or run with explicit path:"; \
		echo "  make CLIENT_CONTEXT=/absolute/or/relative/path build"; \
		echo "Tip: run 'make deps' first if these repos are configured as submodules."; \
		exit 1; \
	fi

ensure-build-contexts: ensure-simulator-context ensure-client-context

# Dispara a descoberta de dispositivos para todos os gateways via API REST.
# Passe ARGS para enviar opções adicionais ao comando (ex: ARGS=\"--gateway-ids=1,2 --dry-run\").
discover-gateways:
	$(COMPOSE) exec -T middleware python manage.py discover_all_gateways --base-url http://localhost:8000 $(ARGS)

discover-devices: discover-gateways

# Carrega o cenário House 2.0 e em seguida descobre os devices nos gateways cadastrados.
seed-house-devices:
	$(MAKE) seed-house ARGS="$(ARGS)"
	$(MAKE) discover-devices ARGS="$(ARGS)"

build: ensure-build-contexts
	# Principal build target: middleware + simulator + client (fast path with cache)
	$(COMPOSE_WITH_CONTEXT) --profile simulator --profile client build

build-refresh: ensure-build-contexts
	# Rebuild checking newer base images (slower than default build)
	$(COMPOSE_WITH_CONTEXT) --profile simulator --profile client build --pull

build-nocache: ensure-build-contexts
	# Full rebuild without cache (slower and uses more disk)
	$(COMPOSE_WITH_CONTEXT) --profile simulator --profile client build --no-cache

up: ensure-build-contexts
	# Principal startup target for the full solution (middleware + simulator + client)
	$(COMPOSE_WITH_CONTEXT) --profile simulator --profile client up -d

down:
	# Bring down full stack including profile services and orphans; keep named volumes by default
	$(COMPOSE_WITH_CONTEXT) --profile simulator --profile client down --remove-orphans

clean:
	# Stop and remove containers, networks and volumes defined in compose, then prune dangling volumes
	$(COMPOSE) down -v --remove-orphans || true
	@echo "Pruning dangling Docker volumes (non-destructive for images)..."
	docker volume prune -f || true

fullclean:
	# Destructive: remove containers, images and volumes for a full reset of the environment
	@echo "FULL CLEAN: stopping compose, removing images and volumes. This is destructive."
	$(COMPOSE) down --rmi all -v --remove-orphans || true
	@echo "Running docker system prune -a --volumes (may free a lot of space)..."
	docker system prune -a --volumes -f || true

prune-safe:
	# Reclaim disk space without removing named volumes and preserving useful build cache
	@echo "SAFE PRUNE: removing stopped containers, unused networks and dangling artifacts."
	docker container prune -f || true
	docker network prune -f || true
	docker image prune -f || true
	docker builder prune -f || true

prune-all:
	# Destructive prune: includes unused volumes (can delete persisted DB data when stack is down)
	@echo "PRUNE ALL: includes unused volumes and may remove persisted data."
	docker system prune -a --volumes -f || true

space-report:
	@echo "Filesystem usage:"
	@df -h / /var/lib/docker 2>/dev/null || df -h /
	@echo "\nFilesystem inode usage:"
	@df -i / /var/lib/docker 2>/dev/null || df -i /
	@echo "\nDocker usage:"
	@docker system df -v

restart:
	$(COMPOSE) restart middleware nginx

logs:
	$(COMPOSE) logs -f --tail=200

migrate:
	$(COMPOSE) exec -T middleware python manage.py migrate

collectstatic:
	$(COMPOSE) exec -T middleware python manage.py collectstatic --noinput

shell:
	$(COMPOSE) exec -T middleware bash

sim-up: ensure-simulator-context
	$(COMPOSE_WITH_CONTEXT) --profile simulator up -d

sim-down:
	$(COMPOSE) stop simulator || true
	$(COMPOSE) rm -f simulator || true

client-build: ensure-client-context
	$(COMPOSE_WITH_CONTEXT) --profile client build client

client-up: ensure-client-context
	$(COMPOSE_WITH_CONTEXT) --profile client up -d client

client-down:
	$(COMPOSE) stop client || true
	$(COMPOSE) rm -f client || true

update:
	@git pull --ff-only || true
	$(COMPOSE_WITH_CONTEXT) pull
	$(COMPOSE_WITH_CONTEXT) --profile simulator --profile client up -d --remove-orphans --build
	$(MAKE) migrate
	$(MAKE) collectstatic

db-backup:
	@echo "Run on host to create DB dump:\n  docker exec -t $$(docker compose -f docker-compose.yml ps -q db) pg_dump -U ${POSTGRES_USER:-postgres} middts > middts.sql"

deploy: update
	@echo "Deploy finished. Verify services with: make logs"

# Restore DB from middts.sql (run on host)
db-restore:
	@echo "Run on host to restore DB dump (middts.sql):"
	@echo "  cat middts.sql | docker exec -i $$(docker compose -f docker-compose.yml ps -q db) psql -U ${POSTGRES_USER:-postgres} middts"

# Carrega o cenário House 2.0 (SystemContext + 8 DTDLModels) via API REST.
# Requer que o middleware esteja UP. Passe ARGS="--force" para recriar modelos existentes.
seed-house:
	$(COMPOSE) exec -T middleware python manage.py load_house_scenario --base-url http://localhost:8000 $(ARGS)

# Simple healthcheck for main services
healthcheck:
	@echo "Checking HTTP endpoints..."
	@curl -fsS --retry 2 --retry-delay 1 --retry-connrefused --max-time 10 http://localhost:8000/ >/dev/null && echo "middleware: OK" || echo "middleware: FAIL"
	@curl -fsS --max-time 5 http://localhost:8001/ >/dev/null && echo "simulator: OK" || echo "simulator: FAIL"
	@curl -fsS --retry 2 --retry-delay 1 --retry-connrefused --max-time 10 http://localhost:8002/ >/dev/null && echo "client: OK" || echo "client: OFF/FAIL"
	@curl -fsS --max-time 5 http://localhost:8082/swagger/index.html >/dev/null && echo "parser: OK" || echo "parser: FAIL"
	@curl -fsS --max-time 5 http://localhost:8086/health >/dev/null && echo "influxdb: OK" || echo "influxdb: FAIL"
	@curl -fsS --max-time 5 http://localhost:7474/ >/dev/null && echo "neo4j: OK" || echo "neo4j: FAIL"
	@docker compose -f docker-compose.yml exec -T db pg_isready -U $${POSTGRES_USER:-postgres} >/dev/null 2>&1 && echo "postgres: OK" || echo "postgres: FAIL"
	@docker compose -f docker-compose.yml exec -T redis redis-cli PING >/dev/null 2>&1 && echo "redis: OK" || echo "redis: FAIL"
	@echo "docker compose ps:"
	@docker compose -f docker-compose.yml ps

# Rollback to a provided image tag (local override). Set ROLLBACK_IMAGE env, e.g. ROLLBACK_IMAGE=myrepo/middleware:20260412
rollback:
	@if [ -z "$(ROLLBACK_IMAGE)" ]; then \
		echo "Set ROLLBACK_IMAGE env var to the image you want to roll back to (e.g. myrepo/middleware:tag)"; exit 1; \
	fi
	@echo "Pulling $(ROLLBACK_IMAGE) and tagging as local middleware image..."
	@docker pull $(ROLLBACK_IMAGE) || true
	@docker tag $(ROLLBACK_IMAGE) middleware-dt_middleware:latest || true
	@docker compose -f docker-compose.yml up -d --no-deps --force-recreate middleware
	@echo "Rollback invoked; verify with: make logs"
