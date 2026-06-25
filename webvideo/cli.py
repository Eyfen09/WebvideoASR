from __future__ import annotations

from dataclasses import replace
import multiprocessing
import threading
import webbrowser
from multiprocessing.process import BaseProcess

import uvicorn

from .app import create_app
from .config import DEFAULT_CONFIG, WebVideoConfig


SHUTDOWN_GRACE_SECONDS = 2.0


def _serve(config: WebVideoConfig) -> None:
    uvicorn.run(
        create_app(config), host=config.host, port=config.port, log_level="info"
    )


def _wait_for_server(process: BaseProcess) -> int:
    try:
        while process.is_alive():
            process.join(0.5)
    except KeyboardInterrupt:
        # The terminal also sends SIGINT to the server child. Give Uvicorn a
        # moment to close SQLite and browser resources, then abandon any
        # uncancellable extractor thread that is still blocking interpreter exit.
        try:
            process.join(SHUTDOWN_GRACE_SECONDS)
        except KeyboardInterrupt:
            pass
        if process.is_alive():
            process.kill()
            process.join()
        return 130
    return int(process.exitcode or 0)


def run(*, cpu: bool = False) -> int:
    config = DEFAULT_CONFIG
    if cpu:
        config = replace(config, device="cpu")
    config.validate()
    url = f"http://{config.host}:{config.port}/"
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_serve,
        args=(config,),
        name="webvideo-server",
    )
    process.start()
    timer: threading.Timer | None = None
    if config.open_browser:
        timer = threading.Timer(0.8, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()
    try:
        return _wait_for_server(process)
    finally:
        if timer is not None:
            timer.cancel()
        process.close()
