"""
Main Entry Point - Initialize components and start server
"""

import asyncio
import logging
import sys
from pathlib import Path

from .db import Database, DEFAULT_DB_PATH
from .process_manager import ProcessManager
from .round_controller import RoundController
from .server import ChatServer, HOST, PORT


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )


async def main() -> None:
    setup_logging()
    log = logging.getLogger(__name__)

    log.info("=" * 50)
    log.info("MultiAgentRoundRunner")
    log.info("=" * 50)

    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    db_path = Path(DEFAULT_DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    db = Database(str(db_path))
    await db.initialize()

    process_manager = ProcessManager()

    round_controller = RoundController(
        db=db,
        process_manager=process_manager,
        broadcast_callback=lambda msg: None,  # replaced after server init
    )

    server = ChatServer(db=db, round_controller=round_controller)
    round_controller.broadcast = server.broadcast

    stop_event = asyncio.Event()

    if sys.platform != 'win32':
        import signal
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)

    log.info(f"Listening on http://{HOST}:{PORT}/")
    await server.run(stop_event)

    await process_manager.stop_current()
    await db.close()
    log.info("Shutdown complete.")


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown requested, exiting...")
    except Exception as e:
        logging.exception(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    run()
