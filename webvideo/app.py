from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .artifacts import ArtifactCleaner, ArtifactDeleteError
from .auth import AuthCoordinator, CookieStore, LoginCacheManager
from .config import DEFAULT_CONFIG, WebVideoConfig
from .integrations.registry import default_registry
from .repository import WebVideoRepository
from .workers import WorkerBusyError, WorkerManager, WorkerPhaseError


TERMINAL_TASK_STATUSES = frozenset(
    {"ready", "completed", "completed_with_errors", "cancelled", "failed"}
)
TRANSCRIPTION_STARTABLE_STATUSES = frozenset(
    {"ready", "completed", "completed_with_errors", "cancelled"}
)


def _can_start_transcription(task: dict[str, Any], busy: bool) -> bool:
    return (
        not busy
        and int(task.get("selected_count") or 0) > 0
        and str(task.get("status") or "") in TRANSCRIPTION_STARTABLE_STATUSES
    )


class URLRequest(BaseModel):
    url: str


class SelectionRequest(BaseModel):
    selected: bool


class BrowserActionRequest(BaseModel):
    action: Literal["continue", "skip"]


class StorageDeleteRequest(BaseModel):
    ids: list[int]


class Runtime:
    def __init__(self, config: WebVideoConfig) -> None:
        config.validate()
        config.prepare_directories()
        self.config = config
        self.repository = WebVideoRepository(config.database_path)
        self.repository.initialize_runtime_session()
        self.cookie_store = CookieStore(config.cookie_file)
        self.auth = AuthCoordinator(
            self.cookie_store,
            poll_interval_seconds=config.qrcode_poll_interval_seconds,
        )
        self.integrations = default_registry(config, self.cookie_store)
        self.login_cache = LoginCacheManager(
            self.cookie_store,
            self.integrations.integrations,
            browser_profile_dir=config.browser_profile_dir,
        )
        self.workers = WorkerManager(config, self.repository)
        self.artifacts = ArtifactCleaner(config, self.repository)

    def close(self) -> None:
        self.repository.close()


def _validate_url(value: str) -> str:
    url = value.strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(422, "请输入完整的 http/https 链接")
    return url


def create_app(
    config: WebVideoConfig = DEFAULT_CONFIG,
    *,
    runtime: Runtime | None = None,
) -> FastAPI:
    state = runtime or Runtime(config)

    def public_task(task: dict[str, Any] | None) -> dict[str, Any] | None:
        if task is None:
            return None
        payload = dict(task)
        payload["running"] = state.workers.is_running(int(task["id"]))
        payload["can_start_transcription"] = _can_start_transcription(
            task,
            state.workers.is_running(),
        )
        return payload

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> Any:
        try:
            yield
        finally:
            await state.workers.close()
            state.close()

    app = FastAPI(
        title="通用网络视频转录", version="0.1.0", lifespan=lifespan
    )
    app.state.runtime = state
    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).parent / "static"),
        name="static",
    )

    @app.middleware("http")
    async def disable_frontend_cache(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
        return response

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        package_dir = Path(__file__).parent
        asset_bytes = b"".join(
            (package_dir / "static" / filename).read_bytes()
            for filename in ("app.js", "styles.css")
        )
        version = hashlib.sha256(asset_bytes).hexdigest()[:12]
        template = (package_dir / "templates" / "index.html").read_text(
            encoding="utf-8"
        )
        return template.replace("__ASSET_VERSION__", version)

    @app.get("/api/tasks/latest")
    def latest_task() -> dict[str, Any]:
        task = state.repository.latest_task()
        return {
            "task": public_task(task),
            "auth": state.repository.auth_snapshot(int(task["id"])) if task else {},
        }

    @app.get("/api/auth/cache")
    async def list_login_cache() -> dict[str, Any]:
        entries = await asyncio.to_thread(state.login_cache.list_entries)
        return {
            "entries": entries,
            "busy": state.workers.is_running(),
        }

    @app.delete("/api/auth/cache/{cache_key}")
    async def clear_login_cache(cache_key: str) -> dict[str, Any]:
        if state.workers.is_running():
            raise HTTPException(409, "任务运行期间不能清除登录缓存，请先停止任务")
        entry = await asyncio.to_thread(state.login_cache.clear, cache_key)
        if entry is None:
            raise HTTPException(404, "该平台没有可清除的登录缓存")
        state.auth.clear_platform(entry.platform)
        return {"ok": True, "platform": entry.platform}

    @app.delete("/api/auth/cache")
    async def clear_all_login_cache() -> dict[str, bool]:
        if state.workers.is_running():
            raise HTTPException(409, "任务运行期间不能清除登录缓存，请先停止任务")
        await asyncio.to_thread(state.login_cache.clear_all)
        state.auth.clear_all()
        return {"ok": True}

    @app.post("/api/tasks", status_code=201)
    async def create_task(payload: URLRequest) -> dict[str, Any]:
        if state.workers.is_running():
            raise HTTPException(409, "已有任务正在运行，请先等待完成或点击停止")
        url = _validate_url(payload.url)
        task_id = state.repository.create_task(url)
        try:
            await state.workers.start_parsing(task_id, url)
        except WorkerBusyError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {
            "task": public_task(state.repository.get_task(task_id)),
            "auth": state.repository.auth_snapshot(task_id),
        }

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: int) -> dict[str, Any]:
        task = state.repository.get_task(task_id)
        if task is None:
            raise HTTPException(404, "任务不存在")
        return {
            "task": public_task(task),
            "auth": state.repository.auth_snapshot(task_id),
        }

    @app.get("/api/tasks/{task_id}/items")
    def list_items(
        task_id: int,
        offset: int = Query(0, ge=0),
        limit: int = Query(config.page_size, ge=1, le=500),
    ) -> dict[str, Any]:
        task = state.repository.get_task(task_id)
        if task is None:
            raise HTTPException(404, "任务不存在")
        return {
            "items": state.repository.list_items(
                task_id, offset=offset, limit=limit
            ),
            "total": task["item_count"],
        }

    @app.put("/api/tasks/{task_id}/items/{item_id}/selection")
    def set_selection(
        task_id: int, item_id: int, payload: SelectionRequest
    ) -> dict[str, bool]:
        state.repository.set_selected(task_id, item_id, payload.selected)
        return {"ok": True}

    @app.put("/api/tasks/{task_id}/selection")
    def set_all_selection(
        task_id: int, payload: SelectionRequest
    ) -> dict[str, bool]:
        state.repository.set_all_selected(task_id, payload.selected)
        return {"ok": True}

    async def stop_phase(task_id: int, phase: str) -> dict[str, bool]:
        task = state.repository.get_task(task_id)
        if task is None:
            raise HTTPException(404, "任务不存在")
        if (
            task["status"] in TERMINAL_TASK_STATUSES
            and not state.workers.is_running(task_id)
        ):
            return {"ok": True}
        try:
            await state.workers.stop(task_id, phase)
        except WorkerPhaseError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/stop-parsing")
    async def stop_parsing(task_id: int) -> dict[str, bool]:
        return await stop_phase(task_id, "parsing")

    @app.post("/api/tasks/{task_id}/stop-transcription")
    async def stop_transcription(task_id: int) -> dict[str, bool]:
        return await stop_phase(task_id, "transcription")

    @app.post("/api/tasks/{task_id}/browser-action")
    def browser_action(
        task_id: int, payload: BrowserActionRequest
    ) -> dict[str, bool]:
        task = state.repository.get_task(task_id)
        if task is None:
            raise HTTPException(404, "任务不存在")
        if task["status"] != "waiting_browser":
            raise HTTPException(409, "当前任务不在等待浏览器确认")
        state.repository.set_browser_action(task_id, payload.action)
        state.repository.set_task_status(task_id, "browser")
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/confirm")
    async def confirm_task(task_id: int) -> dict[str, bool]:
        task = state.repository.get_task(task_id)
        if task is None:
            raise HTTPException(404, "任务不存在")
        if not task["selected_count"]:
            raise HTTPException(409, "请至少选择一个视频")
        if not _can_start_transcription(task, state.workers.is_running()):
            raise HTTPException(409, "当前任务还不能开始转录")
        state.repository.clear_stop(task_id)
        state.repository.set_task_status(task_id, "scheduled")
        try:
            await state.workers.start_transcription(task_id)
        except WorkerBusyError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/retry")
    async def retry_task(task_id: int) -> dict[str, Any]:
        task = state.repository.get_task(task_id)
        if task is None:
            raise HTTPException(404, "任务不存在")
        count = state.repository.reset_failed(task_id)
        if count:
            state.repository.clear_stop(task_id)
            state.repository.set_task_status(task_id, "scheduled")
            try:
                await state.workers.start_transcription(task_id)
            except WorkerBusyError as exc:
                raise HTTPException(409, str(exc)) from exc
        return {"ok": True, "reset": count}

    @app.get("/api/tasks/{task_id}/events")
    async def task_events(task_id: int, request: Request) -> StreamingResponse:
        if state.repository.get_task(task_id) is None:
            raise HTTPException(404, "任务不存在")

        async def stream() -> Any:
            previous = ""
            while not await request.is_disconnected():
                task = state.repository.get_task(task_id)
                payload = json.dumps(
                    {
                        "task": public_task(task),
                        "auth": state.repository.auth_snapshot(task_id),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                if payload != previous:
                    yield f"data: {payload}\n\n"
                    previous = payload
                else:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(0.8)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/items/{item_id}/transcript")
    def transcript(item_id: int) -> FileResponse:
        record = state.repository.transcript_record(item_id)
        if record is None:
            raise HTTPException(404, "视频不存在")
        output = Path(str(record.get("output_path") or ""))
        if not output.is_file():
            raise HTTPException(404, "转录文件不存在")
        return FileResponse(output, media_type="text/plain; charset=utf-8")

    @app.delete("/api/items/{item_id}/artifacts")
    def delete_item_artifacts(item_id: int) -> dict[str, bool]:
        if state.workers.is_running():
            raise HTTPException(409, "任务运行期间不能删除视频数据")
        try:
            state.artifacts.delete(item_id)
        except KeyError as exc:
            raise HTTPException(404, "视频不存在") from exc
        except ArtifactDeleteError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True}

    @app.get("/api/storage/items")
    def storage_items() -> dict[str, Any]:
        pruned = state.artifacts.prune_missing_outputs()
        items = state.repository.list_storage_items()
        return {"items": items, "total": len(items), "pruned": pruned}

    @app.delete("/api/storage/items")
    def delete_storage_items(payload: StorageDeleteRequest) -> dict[str, int]:
        if state.workers.is_running():
            raise HTTPException(409, "任务运行期间不能删除视频数据")
        ids = sorted({int(item_id) for item_id in payload.ids if int(item_id) > 0})
        if not ids:
            raise HTTPException(422, "请选择要删除的视频")
        try:
            deleted = state.artifacts.delete_records(ids)
        except ArtifactDeleteError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"deleted": deleted}

    return app
