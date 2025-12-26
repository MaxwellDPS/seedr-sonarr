# Seedr-Sonarr Proxy

[![CI/CD](https://github.com/example/seedr-sonarr/actions/workflows/ci.yml/badge.svg)](https://github.com/example/seedr-sonarr/actions/workflows/ci.yml)
[![Security Scan](https://github.com/example/seedr-sonarr/actions/workflows/security.yml/badge.svg)](https://github.com/example/seedr-sonarr/actions/workflows/security.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Docker Pulls](https://img.shields.io/docker/pulls/example/seedr-sonarr)](https://hub.docker.com/r/example/seedr-sonarr)

A **qBittorrent API emulation layer** that allows you to use [Seedr.cc](https://www.seedr.cc) as a download client for Sonarr, Radarr, and other \*arr applications.

## 🏗️ Supported Architectures

| Architecture | Tag |
|--------------|-----|
| x86-64 (AMD64) | `amd64` |
| ARM64 (Apple Silicon, Raspberry Pi 4+) | `arm64` |

Multi-arch images are automatically built and published via GitHub Actions. The `latest` tag supports both architectures.

## 🌟 Features

- **Full qBittorrent API Emulation**: Appears as a qBittorrent client to Sonarr/Radarr
- **Seedr.cc Integration**: Uses the excellent [seedrcc](https://github.com/hemantapkh/seedrcc) Python library
- **Docker Ready**: Includes Dockerfile and docker-compose for easy deployment
- **Authentication Options**: Supports both email/password and device code authentication
- **Category Support**: Categories from Sonarr/Radarr are tracked and reported
- **Health Checks**: Built-in health endpoint for monitoring

## 🚀 Quick Start

### Docker (Recommended)

1. **Clone and configure:**
   ```bash
   git clone https://github.com/example/seedr-sonarr.git
   cd seedr-sonarr
   cp .env.example .env
   # Edit .env with your Seedr credentials
   ```

2. **Start the proxy:**
   ```bash
   docker-compose up -d
   ```

3. **Configure Sonarr/Radarr:**
   - Go to Settings → Download Clients → Add
   - Select **qBittorrent**
   - Host: `seedr-sonarr` (or `localhost` if not using Docker network)
   - Port: `8080`
   - Username: `admin`
   - Password: `adminadmin`
   - Category: `sonarr` (or `radarr`)

### Python Installation

```bash
pip install seedr-sonarr

# Start the server
seedr-sonarr serve --email your@email.com --password yourpassword
```

## 📋 Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `SEEDR_EMAIL` | Seedr account email | - |
| `SEEDR_PASSWORD` | Seedr account password | - |
| `SEEDR_TOKEN` | Seedr authentication token (alternative) | - |
| `HOST` | Server bind address | `0.0.0.0` |
| `PORT` | Server port | `8080` |
| `USERNAME` | qBittorrent API username | `admin` |
| `PASSWORD` | qBittorrent API password | `adminadmin` |
| `DOWNLOAD_PATH` | Download directory path | `/downloads` |
| `LOG_LEVEL` | Logging level | `INFO` |

### Authentication Methods

#### 1. Email/Password (Simple)
```bash
export SEEDR_EMAIL=your@email.com
export SEEDR_PASSWORD=yourpassword
seedr-sonarr serve
```

#### 2. Device Code (Recommended)
More secure and provides longer-lasting tokens:

```bash
# Run the auth flow
seedr-sonarr auth --save token.json

# Use the saved token
export SEEDR_TOKEN=$(cat token.json)
seedr-sonarr serve
```

## 🔧 Sonarr/Radarr Configuration

### Adding the Download Client

1. Navigate to **Settings** → **Download Clients**
2. Click the **+** button to add a new client
3. Select **qBittorrent** from the list
4. Configure as follows:

| Setting | Value |
|---------|-------|
| Name | Seedr |
| Host | `seedr-sonarr` or `localhost` |
| Port | `8080` |
| Username | `admin` (or your configured username) |
| Password | `adminadmin` (or your configured password) |
| Category | `sonarr` or `radarr` |
| Remove Completed | ✅ Enabled |

5. Click **Test** to verify the connection
6. Click **Save**

### Path Mapping

Since Seedr is a cloud service, you'll need to set up Remote Path Mappings if Sonarr/Radarr and the proxy see different paths:

1. Go to **Settings** → **Download Clients** → **Remote Path Mappings**
2. Add a mapping:
   - Host: `seedr-sonarr`
   - Remote Path: `/downloads`
   - Local Path: `/path/on/your/server`

## 📡 API Endpoints

The proxy implements the following qBittorrent API endpoints:

### Authentication
- `POST /api/v2/auth/login` - Authenticate
- `POST /api/v2/auth/logout` - Logout

### Application
- `GET /api/v2/app/version` - Get version
- `GET /api/v2/app/webapiVersion` - Get WebAPI version
- `GET /api/v2/app/preferences` - Get preferences
- `GET /api/v2/app/defaultSavePath` - Get default save path

### Torrents
- `GET /api/v2/torrents/info` - Get torrent list
- `GET /api/v2/torrents/properties` - Get torrent properties
- `GET /api/v2/torrents/files` - Get torrent files
- `POST /api/v2/torrents/add` - Add torrent
- `POST /api/v2/torrents/delete` - Delete torrent
- `POST /api/v2/torrents/pause` - Pause (no-op)
- `POST /api/v2/torrents/resume` - Resume (no-op)
- `POST /api/v2/torrents/setCategory` - Set category

### Sync
- `GET /api/v2/sync/maindata` - Get sync data (main endpoint for Sonarr)

### Health
- `GET /health` - Health check endpoint

## 🐳 Docker Deployment

### Pulling the Image

```bash
# From GitHub Container Registry (recommended)
docker pull ghcr.io/example/seedr-sonarr:latest

# Specific architecture
docker pull ghcr.io/example/seedr-sonarr:latest --platform linux/amd64
docker pull ghcr.io/example/seedr-sonarr:latest --platform linux/arm64

# Specific version
docker pull ghcr.io/example/seedr-sonarr:v1.0.0
```

### Using docker-compose (Recommended)

```yaml
services:
  seedr-sonarr:
    image: seedr-sonarr:latest
    container_name: seedr-sonarr
    environment:
      - SEEDR_EMAIL=your@email.com
      - SEEDR_PASSWORD=yourpassword
      - USERNAME=admin
      - PASSWORD=adminadmin
    ports:
      - "8080:8080"
    volumes:
      - ./downloads:/downloads
    restart: unless-stopped
```

### Using Docker directly

```bash
docker build -t seedr-sonarr .

docker run -d \
  --name seedr-sonarr \
  -e SEEDR_EMAIL=your@email.com \
  -e SEEDR_PASSWORD=yourpassword \
  -p 8080:8080 \
  -v ./downloads:/downloads \
  seedr-sonarr
```

### With Sonarr Network

If you're using Sonarr in Docker, make sure they're on the same network:

```yaml
networks:
  media:
    external: true

services:
  seedr-sonarr:
    # ... config ...
    networks:
      - media
```

## 🔍 Troubleshooting

### Connection Test Fails

1. Check that the proxy is running: `curl http://localhost:8080/health`
2. Verify Seedr credentials are correct
3. Check logs: `docker logs seedr-sonarr`

### Downloads Not Appearing

1. Ensure the download path is correctly mounted
2. Check that Seedr has available space
3. Verify the torrent was successfully added to Seedr

### Authentication Issues

1. Try re-authenticating: `seedr-sonarr auth`
2. Check that your Seedr account is active
3. Ensure you're not hitting rate limits

### Logs

Enable debug logging for more information:

```bash
export LOG_LEVEL=DEBUG
seedr-sonarr serve
```

Or in Docker:

```yaml
environment:
  - LOG_LEVEL=DEBUG
```

## ⚠️ Limitations

- **No Seeding**: Seedr doesn't support seeding, so ratio requirements cannot be met
- **No Pause/Resume**: Torrents cannot be paused or resumed
- **Cloud Storage**: Files are stored in Seedr's cloud, not locally (unless you implement download functionality)
- **Rate Limits**: Seedr may have API rate limits for free accounts

## 🤝 Contributing

Contributions are welcome! Please feel free to submit issues and pull requests.

## 📄 License

MIT License - See [LICENSE](LICENSE) for details.

## 🙏 Acknowledgments

- [seedrcc](https://github.com/hemantapkh/seedrcc) - The excellent Python API wrapper for Seedr
- [Sonarr](https://github.com/Sonarr/Sonarr) - The amazing TV show management tool
- [RDT-Client](https://github.com/rogerfar/rdt-client) - Inspiration for the qBittorrent API emulation approach
