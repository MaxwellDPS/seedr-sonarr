# Makefile for Seedr-Sonarr Proxy
# ================================

.PHONY: help build push run test clean lint format dev install docker-login

# Default target
help:
	@echo "Seedr-Sonarr Proxy - Build & Deployment"
	@echo "========================================"
	@echo ""
	@echo "Build targets:"
	@echo "  make docker-login - Login to GHCR using GitHub CLI"
	@echo "  make build        - Build Docker image locally"
	@echo "  make push         - Build and push to GitHub Container Registry"
	@echo "  make run          - Run the Docker container locally"
	@echo ""
	@echo "Development targets:"
	@echo "  make install      - Install dependencies in virtual environment"
	@echo "  make dev          - Run development server"
	@echo "  make test         - Run tests"
	@echo "  make lint         - Run linter"
	@echo "  make format       - Format code"
	@echo "  make clean        - Clean build artifacts"
	@echo ""
	@echo "Docker Compose targets:"
	@echo "  make up           - Start all services (docker-compose.local.yaml)"
	@echo "  make down         - Stop all services"
	@echo "  make logs         - View logs"

# Configuration
IMAGE_NAME ?= ghcr.io/maxwelldps/seedr-sonarr
VERSION ?= $(shell grep '^version' pyproject.toml | head -1 | cut -d'"' -f2)
TAG ?= $(VERSION)
PLATFORMS ?= linux/amd64,linux/arm64

# Login to GHCR using GitHub CLI
docker-login:
	@echo "Logging into GitHub Container Registry..."
	@gh auth token | docker login ghcr.io -u $$(gh api user -q .login) --password-stdin

# Build Docker image locally
build:
	@echo "Building $(IMAGE_NAME):$(TAG)..."
	docker buildx build --load -t $(IMAGE_NAME):$(TAG) -t $(IMAGE_NAME):latest .

# Build multi-platform image
build-multi:
	@echo "Building multi-platform image $(IMAGE_NAME):$(TAG)..."
	docker buildx build \
		--platform $(PLATFORMS) \
		-t $(IMAGE_NAME):$(TAG) \
		-t $(IMAGE_NAME):latest \
		.

# Push to GitHub Container Registry
push: build
	@echo "Pushing $(IMAGE_NAME):$(TAG) to GHCR..."
	@if command -v gh >/dev/null 2>&1; then \
		echo "Using GitHub CLI for authentication..."; \
		gh auth token | docker login ghcr.io -u USERNAME --password-stdin; \
	else \
		echo "GitHub CLI not found, assuming already authenticated"; \
	fi
	docker push $(IMAGE_NAME):$(TAG)
	docker push $(IMAGE_NAME):latest

# Push multi-platform to GHCR
push-multi:
	@echo "Building and pushing multi-platform image $(IMAGE_NAME):$(TAG) to GHCR..."
	@if command -v gh >/dev/null 2>&1; then \
		echo "Using GitHub CLI for authentication..."; \
		gh auth token | docker login ghcr.io -u USERNAME --password-stdin; \
	else \
		echo "GitHub CLI not found, assuming already authenticated"; \
	fi
	docker buildx build \
		--platform $(PLATFORMS) \
		-t $(IMAGE_NAME):$(TAG) \
		-t $(IMAGE_NAME):latest \
		--push \
		.

# Run container locally
run:
	docker run -it --rm \
		-p 8080:8080 \
		-e SEEDR_EMAIL=$${SEEDR_EMAIL} \
		-e SEEDR_PASSWORD=$${SEEDR_PASSWORD} \
		-v $$(pwd)/downloads:/downloads \
		$(IMAGE_NAME):latest

# Development setup
install:
	python3 -m venv .venv
	.venv/bin/pip install -e ".[dev]"
	@echo "Activate with: source .venv/bin/activate"

# Run development server
dev:
	.venv/bin/python -m seedr_sonarr serve --log-level DEBUG

# Run tests
test:
	.venv/bin/pytest tests/ -v

# Run tests with coverage
test-cov:
	.venv/bin/pytest tests/ -v --cov=seedr_sonarr --cov-report=html

# Lint code
lint:
	.venv/bin/ruff check seedr_sonarr tests

# Format code
format:
	.venv/bin/ruff format seedr_sonarr tests

# Clean build artifacts
clean:
	rm -rf build dist *.egg-info .pytest_cache .coverage htmlcov
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true

# Docker Compose commands for local development stack
up:
	docker-compose -f docker-compose.local.yaml up -d

down:
	docker-compose -f docker-compose.local.yaml down

logs:
	docker-compose -f docker-compose.local.yaml logs -f

# Release helpers
release:
	@echo "Creating release for version $(VERSION)..."
	@if command -v gh >/dev/null 2>&1; then \
		gh release create v$(VERSION) --title "v$(VERSION)" --generate-notes; \
	else \
		echo "GitHub CLI not found. Install with: brew install gh"; \
		exit 1; \
	fi

# Version bump helpers
bump-patch:
	@echo "Bumping patch version..."
	@current=$$(grep '^version' pyproject.toml | head -1 | cut -d'"' -f2); \
	major=$$(echo $$current | cut -d. -f1); \
	minor=$$(echo $$current | cut -d. -f2); \
	patch=$$(echo $$current | cut -d. -f3); \
	new="$$major.$$minor.$$((patch + 1))"; \
	sed -i '' "s/version = \"$$current\"/version = \"$$new\"/" pyproject.toml; \
	echo "Version bumped: $$current -> $$new"

bump-minor:
	@echo "Bumping minor version..."
	@current=$$(grep '^version' pyproject.toml | head -1 | cut -d'"' -f2); \
	major=$$(echo $$current | cut -d. -f1); \
	minor=$$(echo $$current | cut -d. -f2); \
	new="$$major.$$((minor + 1)).0"; \
	sed -i '' "s/version = \"$$current\"/version = \"$$new\"/" pyproject.toml; \
	echo "Version bumped: $$current -> $$new"

bump-major:
	@echo "Bumping major version..."
	@current=$$(grep '^version' pyproject.toml | head -1 | cut -d'"' -f2); \
	major=$$(echo $$current | cut -d. -f1); \
	new="$$((major + 1)).0.0"; \
	sed -i '' "s/version = \"$$current\"/version = \"$$new\"/" pyproject.toml; \
	echo "Version bumped: $$current -> $$new"
