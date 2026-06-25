from __future__ import annotations

import asyncio
import multiprocessing
import os
import signal
from dataclasses import dataclass, field
from multiprocessing.process import BaseProcess
from typing import Any, Callable

from .auth import AuthCoordinator, CookieStore
from .config import WebVideoConfig
from .integrations.registry import default_registry
from .pipeline import TranscriptionPipeline
from .repository import WebVideoRepository
from .resolver import Resolver


WORKER_PHASES = frozenset({"parsing", "transcription"})
TERMINAL_TASK_STATUSES = frozenset(
    {"ready", "completed", "completed_with_errors", "cancelled", "failed"}
)


class WorkerBusyError(RuntimeError):
    pass


class WorkerPhaseError(RuntimeError):
    pass


def _prepare_process_group() -> None:
    if hasattr(os, "setsid"):
        os.setsid()
    signal.signal(signal.SIGTERM, lambda *_: None)


def run_resolver_worker(
    config: WebVideoConfig, task_id: int, url: str
) -> None:
    _prepare_process_group()
    repository = WebVideoRepository(config.database_path)
    cookie_store = CookieStore(config.cookie_file)
    auth = AuthCoordinator(
        cookie_store,
        poll_interval_seconds=config.qrcode_poll_interval_seconds,
        snapshot_writer=repository.set_auth_snapshot,
    )
    integrations = default_registry(config, cookie_store)
    resolver = Resolver(
        config,
        repository,
        integrations=integrations,
        auth=auth,
        cookie_store=cookie_store,
    )
    try:
        asyncio.run(resolver.resolve(task_id, url))
    finally:
        repository.close()


def run_transcription_worker(config: WebVideoConfig, task_id: int) -> None:
    _prepare_process_group()
    repository = WebVideoRepository(config.database_path)
    cookie_store = CookieStore(config.cookie_file)
    integrations = default_registry(config, cookie_store)
    pipeline = TranscriptionPipeline(
        config,
        repository,
        integrations=integrations,
    )
    try:
        pipeline.run(task_id)
    finally:
        repository.close()


def _default_process_factory(**kwargs: Any) -> BaseProcess:
    context = multiprocessing.get_context("spawn")
    return context.Process(**kwargs)


def _signal_process_group(pid: int, sent_signal: int) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(pid, sent_signal)
        else:
            os.kill(pid, sent_signal)
    except ProcessLookupError:
        try:
            os.kill(pid, sent_signal)
        except ProcessLookupError:
            pass


@dataclass
class WorkerHandle:
    task_id: int
    phase: str
    process: BaseProcess
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    stop_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    monitor: asyncio.Task[None] | None = None


class WorkerManager:
    def __init__(
        self,
        config: WebVideoConfig,
        repository: WebVideoRepository,
        *,
        process_factory: Callable[..., BaseProcess] = _default_process_factory,
        group_signaler: Callable[[int, int], None] = _signal_process_group,
        grace_seconds: float = 2.0,
    ) -> None:
        self.config = config
        self.repository = repository
        self.process_factory = process_factory
        self.group_signaler = group_signaler
        self.grace_seconds = max(0.0, float(grace_seconds))
        self.handles: dict[int, WorkerHandle] = {}

    def is_running(self, task_id: int | None = None) -> bool:
        if task_id is None:
            return bool(self.handles)
        return task_id in self.handles

    async def start_parsing(self, task_id: int, url: str) -> None:
        await self._start(
            task_id,
            "parsing",
            target=run_resolver_worker,
            args=(self.config, task_id, url),
        )

    async def start_transcription(self, task_id: int) -> None:
        await self._start(
            task_id,
            "transcription",
            target=run_transcription_worker,
            args=(self.config, task_id),
        )

    async def _start(
        self,
        task_id: int,
        phase: str,
        *,
        target: Callable[..., None],
        args: tuple[Any, ...],
    ) -> None:
        if phase not in WORKER_PHASES:
            raise WorkerPhaseError("无效的 Worker 阶段")
        if self.is_running():
            raise WorkerBusyError("已有任务正在运行")
        self.repository.clear_stop(task_id)
        process = self.process_factory(
            target=target,
            args=args,
            name=f"webvideo-{phase}-{task_id}",
            daemon=False,
        )
        try:
            process.start()
        except Exception:
            self.repository.set_task_status(task_id, "failed", "Worker 启动失败")
            raise
        pid = int(process.pid or 0)
        if pid <= 0:
            self.repository.set_task_status(task_id, "failed", "Worker 没有有效 PID")
            raise RuntimeError("Worker 没有有效 PID")
        handle = WorkerHandle(task_id=task_id, phase=phase, process=process)
        self.handles[task_id] = handle
        self.repository.set_task_phase(task_id, phase, worker_pid=pid)
        handle.monitor = asyncio.create_task(self._monitor(handle))

    async def _monitor(self, handle: WorkerHandle) -> None:
        try:
            await asyncio.to_thread(handle.process.join)
            exitcode = handle.process.exitcode
            task = self.repository.get_task(handle.task_id)
            self.handles.pop(handle.task_id, None)
            self.repository.clear_worker(handle.task_id)
            if task is not None and task["stop_requested"]:
                self.repository.set_task_status(handle.task_id, "cancelled")
            elif (
                task is not None
                and task["status"] not in TERMINAL_TASK_STATUSES
            ):
                detail = (
                    "Worker 未写入完成状态"
                    if exitcode == 0
                    else f"Worker 异常退出 ({exitcode})"
                )
                self.repository.set_task_status(handle.task_id, "failed", detail)
        finally:
            self.handles.pop(handle.task_id, None)
            try:
                handle.process.close()
            except (OSError, ValueError):
                pass
            handle.finished.set()

    async def stop(self, task_id: int, phase: str) -> None:
        if phase not in WORKER_PHASES:
            raise WorkerPhaseError("无效的 Worker 阶段")
        task = self.repository.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        if str(task.get("phase") or "") != phase:
            raise WorkerPhaseError("当前任务不在请求停止的阶段")
        handle = self.handles.get(task_id)
        self.repository.request_stop(task_id)
        if handle is None:
            self.repository.clear_worker(task_id)
            self.repository.set_task_status(task_id, "cancelled")
            return
        if handle.phase != phase:
            raise WorkerPhaseError("当前 Worker 不在请求停止的阶段")
        async with handle.stop_lock:
            if handle.finished.is_set():
                return
            pid = int(handle.process.pid or 0)
            if pid > 0:
                self.group_signaler(pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(
                    handle.finished.wait(), timeout=self.grace_seconds
                )
            except TimeoutError:
                if pid > 0:
                    self.group_signaler(pid, signal.SIGKILL)
                await handle.finished.wait()

    async def close(self) -> None:
        handles = tuple(self.handles.values())
        if not handles:
            return
        await asyncio.gather(
            *(self.stop(handle.task_id, handle.phase) for handle in handles),
            return_exceptions=True,
        )
