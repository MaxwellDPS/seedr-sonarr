#!/usr/bin/env bash
# Build and push Docker image to GitHub Container Registry
# Usage: ./scripts/build-push.sh [--multi] [--tag TAG]

set -euo pipefail

# Configuration
IMAGE_NAME="${IMAGE_NAME:-ghcr.io/maxwelldps/seedr-sonarr}"
VERSION=$(grep '^version' pyproject.toml | head -1 | cut -d'"' -f2)
TAG="${TAG:-$VERSION}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
MULTI_PLATFORM=false

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --multi)
            MULTI_PLATFORM=true
            shift
            ;;
        --tag)
            TAG="$2"
            shift 2
            ;;
        --help|-h)
            echo "Usage: $0 [--multi] [--tag TAG]"
            echo ""
            echo "Options:"
            echo "  --multi    Build multi-platform image (amd64 + arm64)"
            echo "  --tag TAG  Override version tag (default: from pyproject.toml)"
            echo ""
            echo "Environment variables:"
            echo "  IMAGE_NAME   Docker image name (default: ghcr.io/maxwelldps/seedr-sonarr)"
            echo "  PLATFORMS    Platforms for multi-arch build (default: linux/amd64,linux/arm64)"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

echo "====================================="
echo "Seedr-Sonarr Docker Build & Push"
echo "====================================="
echo "Image: ${IMAGE_NAME}"
echo "Tag: ${TAG}"
echo "Multi-platform: ${MULTI_PLATFORM}"
if [ "$MULTI_PLATFORM" = true ]; then
    echo "Platforms: ${PLATFORMS}"
fi
echo "====================================="

# Authenticate with GHCR using GitHub CLI if available
authenticate_ghcr() {
    if command -v gh &> /dev/null; then
        echo "Authenticating with GHCR using GitHub CLI..."
        echo "$(gh auth token)" | docker login ghcr.io -u "$(gh api user --jq '.login')" --password-stdin
    else
        echo "GitHub CLI not found. Checking for existing authentication..."
        if ! docker pull ghcr.io/library/alpine:latest &> /dev/null 2>&1; then
            echo "Not authenticated to GHCR. Please either:"
            echo "  1. Install gh CLI: brew install gh && gh auth login"
            echo "  2. Or manually: docker login ghcr.io -u YOUR_USERNAME -p YOUR_PAT"
            exit 1
        fi
    fi
}

# Build single-platform image
build_single() {
    echo "Building single-platform image..."
    docker build \
        -t "${IMAGE_NAME}:${TAG}" \
        -t "${IMAGE_NAME}:latest" \
        --label "org.opencontainers.image.version=${TAG}" \
        --label "org.opencontainers.image.created=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        .
}

# Build and push multi-platform image
build_multi() {
    echo "Building multi-platform image..."

    # Ensure buildx is available
    if ! docker buildx version &> /dev/null; then
        echo "Docker buildx not available. Please install Docker Desktop or enable buildx."
        exit 1
    fi

    # Create builder if it doesn't exist
    if ! docker buildx inspect seedr-builder &> /dev/null; then
        echo "Creating buildx builder..."
        docker buildx create --name seedr-builder --use
    else
        docker buildx use seedr-builder
    fi

    docker buildx build \
        --platform "${PLATFORMS}" \
        -t "${IMAGE_NAME}:${TAG}" \
        -t "${IMAGE_NAME}:latest" \
        --label "org.opencontainers.image.version=${TAG}" \
        --label "org.opencontainers.image.created=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        --push \
        .
}

# Push single-platform image
push_single() {
    echo "Pushing images..."
    docker push "${IMAGE_NAME}:${TAG}"
    docker push "${IMAGE_NAME}:latest"
}

# Main execution
main() {
    # Change to project root
    cd "$(dirname "$0")/.."

    # Authenticate
    authenticate_ghcr

    if [ "$MULTI_PLATFORM" = true ]; then
        build_multi
    else
        build_single
        push_single
    fi

    echo ""
    echo "====================================="
    echo "Successfully pushed:"
    echo "  - ${IMAGE_NAME}:${TAG}"
    echo "  - ${IMAGE_NAME}:latest"
    echo "====================================="
}

main
