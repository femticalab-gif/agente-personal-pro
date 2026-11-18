# marc-agent — tareas habituales. En el servidor solo hace falta Docker (+ openssl para init-keys).
SHELL := /bin/bash
COMPOSE ?= docker compose
UV ?= uv

.DEFAULT_GOAL := help
.PHONY: help install test lint format run docker-build docker-up docker-down logs ps \
        backup restore restore-drill selftest selftest-full google-auth init-keys config update-deps

help: ## Muestra esta ayuda
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-15s %s\n", $$1, $$2}'

# --- desarrollo (equipo local, con uv) -------------------------------------------------
install: ## Instala dependencias exactas del lockfile (incluye dev)
	$(UV) sync --frozen

test: ## Tests con cobertura (mínimo 85 %)
	$(UV) run pytest --cov=marc_agent --cov-report=term-missing

lint: ## Ruff (lint + formato) y mypy estricto
	$(UV) run ruff check src tests
	$(UV) run ruff format --check src tests
	$(UV) run mypy

format: ## Formatea el código
	$(UV) run ruff format src tests
	$(UV) run ruff check --fix src tests

run: ## Ejecuta el agente en local (requiere .env exportado)
	$(UV) run marc-agent --config config/config.yaml run

update-deps: ## Actualización mensual de dependencias (ver README §5)
	$(UV) lock --upgrade
	$(UV) sync
	$(MAKE) lint test

# --- servidor (Docker) -----------------------------------------------------------------------
config: ## Crea config/config.yaml desde el ejemplo si no existe
	@test -f config/config.yaml || cp config/config.example.yaml config/config.yaml
	@echo "Edita config/config.yaml (perfil, horarios, límites)."

init-keys: ## Genera MARC_DATA/BACKUP/EXPORT_KEY en .env sin mostrarlas
	./scripts/init_keys.sh .env

docker-build: ## Construye las imágenes
	$(COMPOSE) build --pull

docker-up: config ## Arranca (o actualiza) los servicios en segundo plano
	@test -f .env || (echo "Falta .env (cp .env.example .env && chmod 600 .env)"; exit 1)
	$(COMPOSE) up -d --build
	$(COMPOSE) ps

docker-down: ## Para los servicios
	$(COMPOSE) down

ps: ## Estado de los contenedores
	$(COMPOSE) ps

logs: ## Logs del agente
	$(COMPOSE) logs -f --tail=200 agent

google-auth: ## Autoriza Google (OAuth) de forma interactiva
	$(COMPOSE) run --rm agent google-auth

selftest: ## Autodiagnóstico (sin coste de modelo)
	$(COMPOSE) exec agent marc-agent selftest

selftest-full: ## Autodiagnóstico incluyendo una prueba real del agente y de Brave
	$(COMPOSE) exec agent marc-agent selftest --agent --brave

backup: ## Backup cifrado inmediato
	$(COMPOSE) exec agent marc-agent backup

restore-drill: ## Verifica que el último backup se descifra y es íntegro
	$(COMPOSE) exec agent marc-agent verify-backup

restore: ## Restaura un backup (FILE=/data/backups/backup-....tar.enc, por defecto el último)
	$(COMPOSE) stop agent watchdog
	$(COMPOSE) run --rm --no-deps agent restore $(if $(FILE),--file $(FILE),)
	$(COMPOSE) start agent watchdog
