"""
Command Line Interface for Seedr-Sonarr Proxy
Full lifecycle management with persistence and multi-instance support.
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
        description="Seedr-Sonarr Proxy - qBittorrent API emulation for Seedr.cc with full lifecycle management",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Start the server with persistence
  seedr-sonarr serve --port 8080 --persist

  # Start with JSON logging
  seedr-sonarr serve --log-format json --log-file /var/log/seedr-sonarr.log

  # Test connection to Seedr
  seedr-sonarr test --email user@example.com --password mypassword

  # Authorize via device code (recommended)
  seedr-sonarr auth

  # View persisted state
  seedr-sonarr state --db seedr_state.db

  # Clear queue
  seedr-sonarr queue clear --db seedr_state.db

Environment Variables:
  SEEDR_EMAIL           - Seedr account email
  SEEDR_PASSWORD        - Seedr account password
  SEEDR_TOKEN           - Saved authentication token
  HOST                  - Server bind address (default: 0.0.0.0)
  PORT                  - Server port (default: 8080)
  USERNAME              - qBittorrent API username (default: admin)
  PASSWORD              - qBittorrent API password (default: adminadmin)
  DOWNLOAD_PATH         - Download directory (default: /downloads)
  STATE_FILE            - SQLite database for persistence (default: seedr_state.db)
  PERSIST_STATE         - Enable state persistence (default: true)
  LOG_LEVEL             - Logging level (default: INFO)
  LOG_FILE              - Log file path (enables rotation)
  LOG_FORMAT            - Log format: text or json (default: text)
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
        "--log-file", help="Log file path (enables rotation)"
    )
    serve_parser.add_argument(
        "--log-format", choices=["text", "json"], default="text",
        help="Log format: text or json"
    )
    serve_parser.add_argument(
        "--state-file", default="seedr_state.db",
        help="SQLite database for state persistence"
    )
    serve_parser.add_argument(
        "--no-persist", action="store_true",
        help="Disable state persistence"
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
    list_parser.add_argument(
        "--instance", "-i", help="Filter by instance ID"
    )

    # State command
    state_parser = subparsers.add_parser("state", help="View persisted state")
    state_parser.add_argument(
        "--db", default="seedr_state.db", help="SQLite database path"
    )
    state_parser.add_argument(
        "--torrents", action="store_true", help="Show torrents"
    )
    state_parser.add_argument(
        "--queue", action="store_true", help="Show queue"
    )
    state_parser.add_argument(
        "--categories", action="store_true", help="Show categories"
    )
    state_parser.add_argument(
        "--stats", action="store_true", help="Show statistics"
    )

    # Queue command
    queue_parser = subparsers.add_parser("queue", help="Manage queue")
    queue_subparsers = queue_parser.add_subparsers(dest="queue_command")

    queue_list = queue_subparsers.add_parser("list", help="List queue")
    queue_list.add_argument("--db", default="seedr_state.db", help="Database path")

    queue_clear = queue_subparsers.add_parser("clear", help="Clear queue")
    queue_clear.add_argument("--db", default="seedr_state.db", help="Database path")

    # Logs command
    logs_parser = subparsers.add_parser("logs", help="View activity logs")
    logs_parser.add_argument(
        "--db", default="seedr_state.db", help="SQLite database path"
    )
    logs_parser.add_argument(
        "--limit", "-n", type=int, default=50, help="Number of entries"
    )
    logs_parser.add_argument(
        "--level", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Minimum log level"
    )
    logs_parser.add_argument(
        "--hash", help="Filter by torrent hash"
    )

    args = parser.parse_args()

    if args.command == "serve":
        run_server(args)
    elif args.command == "auth":
        asyncio.run(run_auth(args))
    elif args.command == "test":
        asyncio.run(run_test(args))
    elif args.command == "list":
        asyncio.run(run_list(args))
    elif args.command == "state":
        asyncio.run(run_state(args))
    elif args.command == "queue":
        asyncio.run(run_queue(args))
    elif args.command == "logs":
        asyncio.run(run_logs(args))
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
    os.environ["STATE_FILE"] = args.state_file
    os.environ["PERSIST_STATE"] = "false" if args.no_persist else "true"

    if args.log_file:
        os.environ["LOG_FILE"] = args.log_file
    os.environ["LOG_FORMAT"] = args.log_format

    logger.info(f"Starting Seedr-Sonarr proxy on {args.host}:{args.port}")
    logger.info(f"Download path: {args.download_path}")
    logger.info(f"State persistence: {'disabled' if args.no_persist else args.state_file}")
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

            print(f"\n  Successfully authenticated as: {settings.account.username}")

            if args.save:
                with open(args.save, "w") as f:
                    f.write(token)
                print(f"  Token saved to: {args.save}")
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
            print(f"  {message}")

            # Get some stats
            settings = await client.get_settings()
            if settings:
                used = settings.get("space_used", 0)
                total = settings.get("space_max", 0)
                queue_size = settings.get("queue_size", 0)
                if total > 0:
                    pct = (used / total) * 100
                    print(f"  Space: {used / 1e9:.2f} GB / {total / 1e9:.2f} GB ({pct:.1f}%)")
                if queue_size > 0:
                    print(f"  Queue: {queue_size} torrents waiting")
        else:
            print(f"  Connection failed: {message}")
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

        # Filter by instance if specified
        if args.instance:
            torrents = [t for t in torrents if t.instance_id == args.instance]

        if not torrents:
            print("No torrents found.")
            return

        print(f"\nFound {len(torrents)} torrent(s):\n")
        print(f"{'Name':<40} {'Size':>10} {'Progress':>8} {'Phase':<20} {'Instance':<15}")
        print("-" * 100)

        for t in torrents:
            size_str = f"{t.size / 1e6:.1f}MB" if t.size < 1e9 else f"{t.size / 1e9:.2f}GB"
            progress_str = f"{t.progress * 100:.1f}%"
            name = t.name[:37] + "..." if len(t.name) > 40 else t.name
            phase = t.phase.value if hasattr(t.phase, 'value') else str(t.phase)
            instance = t.instance_id or "default"
            print(f"{name:<40} {size_str:>10} {progress_str:>8} {phase:<20} {instance:<15}")

    finally:
        await client.close()


async def run_state(args):
    """View persisted state."""
    import os

    if not os.path.exists(args.db):
        print(f"Database not found: {args.db}")
        sys.exit(1)

    from .persistence import PersistenceManager

    pm = PersistenceManager(args.db)
    await pm.initialize()

    try:
        # Default to showing stats if nothing specified
        show_all = not (args.torrents or args.queue or args.categories or args.stats)

        if args.stats or show_all:
            stats = await pm.get_stats()
            print("\n=== Database Statistics ===")
            for table, count in stats.items():
                print(f"  {table}: {count}")

        if args.torrents or show_all:
            torrents = await pm.get_torrents()
            print(f"\n=== Torrents ({len(torrents)}) ===")
            if torrents:
                print(f"{'Hash':<20} {'Name':<30} {'Phase':<20} {'Instance':<15}")
                print("-" * 90)
                for t in torrents:
                    hash_short = t.hash[:17] + "..." if len(t.hash) > 20 else t.hash
                    name = t.name[:27] + "..." if len(t.name) > 30 else t.name
                    print(f"{hash_short:<20} {name:<30} {t.phase:<20} {t.instance_id or 'default':<15}")

        if args.queue or show_all:
            queue = await pm.get_queued()
            print(f"\n=== Queue ({len(queue)}) ===")
            if queue:
                print(f"{'ID':<10} {'Name':<40} {'Instance':<15} {'Retries':<8}")
                print("-" * 80)
                for q in queue:
                    name = q.name[:37] + "..." if len(q.name) > 40 else q.name
                    print(f"{q.id:<10} {name:<40} {q.instance_id or 'default':<15} {q.retry_count:<8}")

        if args.categories or show_all:
            categories = await pm.get_categories()
            print(f"\n=== Categories ({len(categories)}) ===")
            if categories:
                print(f"{'Name':<20} {'Save Path':<40} {'Instance':<15}")
                print("-" * 80)
                for name, cat in categories.items():
                    print(f"{name:<20} {cat.save_path:<40} {cat.instance_id or 'default':<15}")

    finally:
        await pm.close()


async def run_queue(args):
    """Manage queue."""
    import os

    if not os.path.exists(args.db):
        print(f"Database not found: {args.db}")
        sys.exit(1)

    from .persistence import PersistenceManager

    pm = PersistenceManager(args.db)
    await pm.initialize()

    try:
        if args.queue_command == "list":
            queue = await pm.get_queued()
            if not queue:
                print("Queue is empty.")
                return

            print(f"\nQueue ({len(queue)} items):\n")
            print(f"{'Pos':<5} {'ID':<10} {'Name':<40} {'Instance':<15} {'Retries':<8}")
            print("-" * 85)
            for i, q in enumerate(queue, 1):
                name = q.name[:37] + "..." if len(q.name) > 40 else q.name
                print(f"{i:<5} {q.id:<10} {name:<40} {q.instance_id or 'default':<15} {q.retry_count:<8}")

        elif args.queue_command == "clear":
            count = await pm.clear_queue()
            print(f"Cleared {count} items from queue.")

    finally:
        await pm.close()


async def run_logs(args):
    """View activity logs."""
    import os
    from datetime import datetime

    if not os.path.exists(args.db):
        print(f"Database not found: {args.db}")
        sys.exit(1)

    from .persistence import PersistenceManager

    pm = PersistenceManager(args.db)
    await pm.initialize()

    try:
        logs = await pm.get_activity_log(
            limit=args.limit,
            level=args.level,
            torrent_hash=args.hash,
        )

        if not logs:
            print("No activity logs found.")
            return

        print(f"\nActivity Log ({len(logs)} entries):\n")

        for entry in logs:
            ts = datetime.fromtimestamp(entry.timestamp).strftime("%Y-%m-%d %H:%M:%S")
            level = entry.level
            action = entry.action
            name = entry.torrent_name or ""
            details = entry.details or ""

            # Color coding for levels (if terminal supports it)
            level_colors = {
                "DEBUG": "\033[36m",    # Cyan
                "INFO": "\033[32m",     # Green
                "WARNING": "\033[33m",  # Yellow
                "ERROR": "\033[31m",    # Red
            }
            reset = "\033[0m"
            color = level_colors.get(level, "")

            print(f"{ts} {color}[{level:7}]{reset} {action}: {name}")
            if details:
                print(f"           {details}")

    finally:
        await pm.close()


if __name__ == "__main__":
    main()
