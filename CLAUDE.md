# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Seedr-Sonarr is a qBittorrent API emulation layer that allows Sonarr/Radarr to use Seedr.cc as a download client. It translates qBittorrent Web API calls to Seedr API operations via the `seedrcc` Python library.

## Commands

```bash
# Setup
make install                 # Create venv and install with dev dependencies
source .venv/bin/activate    # Activate virtual environment

# Development
make dev                     # Run server with DEBUG logging
make test                    # Run tests
make test-cov                # Run tests with coverage report
make lint                    # Run ruff linter
make format                  # Format code with ruff

# Run single test
.venv/bin/pytest tests/test_state.py -v
.venv/bin/pytest tests/test_persistence.py::test_save_and_get_torrent -v

# Docker
make build                   # Build local image
make up                      # Start with docker-compose.local.yaml
make down                    # Stop services
make logs                    # View logs
```

## Architecture

### Core Components

**server.py** - FastAPI application implementing qBittorrent Web API endpoints. Entry point for all HTTP requests from Sonarr/Radarr. Uses a lifespan manager to initialize all services. Global settings loaded via pydantic-settings from environment variables.

**seedr_client.py** - `SeedrClientWrapper` class that interfaces with Seedr.cc. Handles:
- Torrent lifecycle (add, progress tracking, completion, deletion)
- Storage management and queueing when Seedr is full
- Auto-download from Seedr to local storage
- Optional QNAP Download Station integration
- Retry logic with circuit breaker pattern

**state.py** - `StateManager` provides centralized, thread-safe state management with optional persistence. Tracks torrents through phases:
- `TorrentPhase` enum: QUEUED_STORAGE → FETCHING_METADATA → DOWNLOADING_TO_SEEDR → SEEDR_PROCESSING → SEEDR_COMPLETE → QUEUED_LOCAL → DOWNLOADING_TO_LOCAL → COMPLETED

**persistence.py** - `PersistenceManager` for SQLite-based state persistence. Survives restarts. Tables: torrents, queued_torrents, categories, hash_mappings, local_downloads, qnap_pending, activity_log.

**retry.py** - Resilience patterns: `RetryHandler`, `CircuitBreaker`, `RateLimiter`, `ResilientExecutor`.

**cli.py** - Command-line interface with subcommands: serve, auth, test, list, state, queue, logs.

### Data Flow

1. Sonarr/Radarr sends qBittorrent API request → server.py
2. Server translates to Seedr operations → seedr_client.py
3. State changes tracked → state.py
4. State persisted → persistence.py (if enabled)
5. Server returns qBittorrent-compatible response

### Multi-Instance Support

Categories from *arr apps (e.g., "radarr-4k", "tv-sonarr") are used to track which instance owns a torrent. The `extract_instance_id()` function parses categories to identify instances.

### qBittorrent State Mapping

Internal `TorrentPhase` values map to qBittorrent states via `PHASE_TO_QBIT_STATE` dict in state.py. This mapping ensures Sonarr/Radarr correctly interprets torrent status.

## Testing

Tests use pytest with pytest-asyncio. Key fixtures in `tests/conftest.py`:
- `mock_seedr_client` - Patches Seedr client for isolation
- `persistence_manager` - Creates temp SQLite DB
- `state_manager` / `state_manager_with_persistence`
- `sample_*` fixtures for test data

Run with: `pytest tests/ -v` or `make test`

## Environment Variables

Key settings (see server.py `Settings` class):
- `SEEDR_EMAIL`, `SEEDR_PASSWORD`, `SEEDR_TOKEN` - Seedr credentials
- `HOST`, `PORT` - Server binding (default: 0.0.0.0:8080)
- `USERNAME`, `PASSWORD` - qBittorrent API auth (default: admin/adminadmin)
- `DOWNLOAD_PATH` - Local download directory
- `CONFIG_PATH` - Path for state DB and config
- `PERSIST_STATE` - Enable SQLite persistence (default: true)
- `AUTO_DOWNLOAD` - Auto-download from Seedr to local
- `QNAP_ENABLED` - Enable QNAP Download Station integration
