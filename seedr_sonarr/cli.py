"""
Command Line Interface for Seedr-Sonarr Proxy
"""

import argparse
import asyncio
import logging
import sys
from typing import Optional

logger = logging.getLogger(__name__)


def setup_logging(level: str = "INFO"):
    """Configure logging."""
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Seedr-Sonarr Proxy - qBittorrent API emulation for Seedr.cc",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Start the server
  seedr-sonarr serve --port 8080

  # Test connection to Seedr
  seedr-sonarr test --email user@example.com --password mypassword

  # Authorize via device code (recommended)
  seedr-sonarr auth

Environment Variables:
  SEEDR_EMAIL       - Seedr account email
  SEEDR_PASSWORD    - Seedr account password  
  SEEDR_TOKEN       - Saved authentication token
  HOST              - Server bind address (default: 0.0.0.0)
  PORT              - Server port (default: 8080)
  USERNAME          - qBittorrent API username (default: admin)
  PASSWORD          - qBittorrent API password (default: adminadmin)
  DOWNLOAD_PATH     - Download directory (default: /downloads)
  LOG_LEVEL         - Logging level (default: INFO)
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Serve command
    serve_parser = subparsers.add_parser("serve", help="Start the proxy server")
    serve_parser.add_argument(
        "--host", "-H", default="0.0.0.0", help="Host to bind to"
    )
    serve_parser.add_argument(
        "--port", "-p", type=int, default=8080, help="Port to listen on"
    )
    serve_parser.add_argument(
        "--email", "-e", help="Seedr email (or use SEEDR_EMAIL env var)"
    )
    serve_parser.add_argument(
        "--password", help="Seedr password (or use SEEDR_PASSWORD env var)"
    )
    serve_parser.add_argument(
        "--token", "-t", help="Seedr token (or use SEEDR_TOKEN env var)"
    )
    serve_parser.add_argument(
        "--username", "-u", default="admin", help="API username"
    )
    serve_parser.add_argument(
        "--api-password", default="adminadmin", help="API password"
    )
    serve_parser.add_argument(
        "--download-path", "-d", default="/downloads", help="Download path"
    )
    serve_parser.add_argument(
        "--log-level", "-l", default="INFO", help="Log level"
    )
    serve_parser.add_argument(
        "--reload", action="store_true", help="Enable auto-reload (dev mode)"
    )

    # Auth command
    auth_parser = subparsers.add_parser(
        "auth", help="Authenticate with Seedr via device code"
    )
    auth_parser.add_argument(
        "--save", "-s", help="File to save token to"
    )

    # Test command
    test_parser = subparsers.add_parser("test", help="Test Seedr connection")
    test_parser.add_argument("--email", "-e", help="Seedr email")
    test_parser.add_argument("--password", help="Seedr password")
    test_parser.add_argument("--token", "-t", help="Seedr token")

    # List command
    list_parser = subparsers.add_parser("list", help="List torrents in Seedr")
    list_parser.add_argument("--email", "-e", help="Seedr email")
    list_parser.add_argument("--password", help="Seedr password")
    list_parser.add_argument("--token", "-t", help="Seedr token")

    args = parser.parse_args()

    if args.command == "serve":
        run_server(args)
    elif args.command == "auth":
        asyncio.run(run_auth(args))
    elif args.command == "test":
        asyncio.run(run_test(args))
    elif args.command == "list":
        asyncio.run(run_list(args))
    else:
        parser.print_help()
        sys.exit(1)


def run_server(args):
    """Run the proxy server."""
    import os
    import uvicorn

    setup_logging(args.log_level)

    # Set environment variables for the server
    if args.email:
        os.environ["SEEDR_EMAIL"] = args.email
    if args.password:
        os.environ["SEEDR_PASSWORD"] = args.password
    if args.token:
        os.environ["SEEDR_TOKEN"] = args.token
    
    os.environ["HOST"] = args.host
    os.environ["PORT"] = str(args.port)
    os.environ["USERNAME"] = args.username
    os.environ["PASSWORD"] = args.api_password
    os.environ["DOWNLOAD_PATH"] = args.download_path
    os.environ["LOG_LEVEL"] = args.log_level

    logger.info(f"Starting Seedr-Sonarr proxy on {args.host}:{args.port}")
    logger.info(f"Download path: {args.download_path}")
    logger.info(f"API credentials: {args.username}:{'*' * len(args.api_password)}")

    uvicorn.run(
        "seedr_sonarr.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level.lower(),
    )


async def run_auth(args):
    """Run device code authentication flow."""
    setup_logging("INFO")

    try:
        from seedrcc import Seedr

        print("\n=== Seedr Device Authentication ===\n")
        
        # Get device code
        codes = Seedr.get_device_code()
        
        print(f"Please visit: {codes.verification_url}")
        print(f"Enter this code: {codes.user_code}")
        print()
        input("Press Enter after authorizing the device...")

        # Create client and get token
        with Seedr.from_device_code(codes.device_code) as client:
            settings = client.get_settings()
            token = client.token.to_json()
            
            print(f"\n✓ Successfully authenticated as: {settings.account.username}")
            
            if args.save:
                with open(args.save, "w") as f:
                    f.write(token)
                print(f"✓ Token saved to: {args.save}")
            else:
                print("\nYour token (save this for later use):")
                print("-" * 50)
                print(token)
                print("-" * 50)
                print("\nSet this as SEEDR_TOKEN environment variable or use --token flag")

    except ImportError:
        print("Error: seedrcc library not installed. Run: pip install seedrcc")
        sys.exit(1)
    except Exception as e:
        print(f"Error during authentication: {e}")
        sys.exit(1)


async def run_test(args):
    """Test Seedr connection."""
    setup_logging("INFO")

    from .seedr_client import SeedrClientWrapper

    client = SeedrClientWrapper(
        email=args.email,
        password=args.password,
        token=args.token,
    )

    try:
        success, message = await client.test_connection()
        if success:
            print(f"✓ {message}")
            
            # Get some stats
            settings = await client.get_settings()
            if settings:
                used = settings.get("space_used", 0)
                total = settings.get("space_max", 0)
                if total > 0:
                    pct = (used / total) * 100
                    print(f"  Space: {used / 1e9:.2f} GB / {total / 1e9:.2f} GB ({pct:.1f}%)")
        else:
            print(f"✗ Connection failed: {message}")
            sys.exit(1)
    finally:
        await client.close()


async def run_list(args):
    """List torrents in Seedr."""
    setup_logging("INFO")

    from .seedr_client import SeedrClientWrapper

    client = SeedrClientWrapper(
        email=args.email,
        password=args.password,
        token=args.token,
    )

    try:
        await client.initialize()
        torrents = await client.get_torrents()

        if not torrents:
            print("No torrents found.")
            return

        print(f"\nFound {len(torrents)} torrent(s):\n")
        print(f"{'Name':<50} {'Size':>12} {'Progress':>10} {'State':<15}")
        print("-" * 90)

        for t in torrents:
            size_str = f"{t.size / 1e6:.1f} MB" if t.size < 1e9 else f"{t.size / 1e9:.2f} GB"
            progress_str = f"{t.progress * 100:.1f}%"
            name = t.name[:47] + "..." if len(t.name) > 50 else t.name
            print(f"{name:<50} {size_str:>12} {progress_str:>10} {t.state.name:<15}")

    finally:
        await client.close()


if __name__ == "__main__":
    main()
