from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from asr_core.config import ASRConfig, Device
from asr_core.engine import create_session, transcribe_audio_text

from .config import WebVideoConfig
from .downloader import DownloadCancelled, MediaDownloadError, MediaDownloader
from .renderer import render_record
from .repository import WebVideoRepository
from .integrations.registry import IntegrationRegistry


class TranscriptionPipeline:
    def __init__(
        self,
        config: WebVideoConfig,
        repository: WebVideoRepository,
        *,
        downloader: MediaDownloader | None = None,
        session_factory: Any = create_session,
        asr_config: ASRConfig | None = None,
        integrations: IntegrationRegistry | None = None,
    ) -> None:
        self.config = config
        self.repository = repository
        self.downloader = downloader or MediaDownloader(config)
        self.session_factory = session_factory
        self.asr_config = asr_config
        self.integrations = integrations

    def _refresh_download_candidate(
        self, item: dict[str, Any]
    ) -> dict[str, Any]:
        if self.integrations is None:
            return item
        integration = self.integrations.match(
            str(item.get("webpage_url") or item.get("source_page") or "")
        )
        refresh = getattr(integration, "refresh_download", None)
        if not callable(refresh):
            return item
        try:
            import asyncio
            import inspect

            if inspect.iscoroutinefunction(refresh):
                candidate = asyncio.run(refresh(item))
            else:
                candidate = refresh(item)
        except Exception:
            return item
        if candidate is None:
            return item
        refreshed = self.repository.refresh_item_media(int(item["id"]), candidate)
        return refreshed or item

    def _download_one(self, task_id: int, item: dict[str, Any]) -> list[Path]:
        item = self._refresh_download_candidate(item)
        item_id = int(item["id"])
        cache_dir = self.downloader.item_cache_dir(item_id)
        self.repository.update_item(
            item_id,
            status="downloading",
            progress=0,
            audio_cache_path=str(cache_dir),
        )
        return self.downloader.download(
            item,
            on_progress=lambda value: self.repository.update_item(
                item_id, status="downloading", progress=value
            ),
            should_stop=lambda: self.repository.should_stop(task_id),
        )

    def _restore_completed(self, item: dict[str, Any]) -> bool:
        if item["status"] != "completed":
            return False
        record = self.repository.transcript_record(int(item["id"]))
        if record is None or not str(record.get("transcript_text") or "").strip():
            return False
        output_value = str(record.get("output_path") or "")
        if output_value and Path(output_value).is_file():
            return True
        destination = render_record(record, self.config.output_dir)
        self.repository.store_transcript(
            int(item["id"]),
            str(record["transcript_text"]),
            str(record.get("detected_language") or ""),
            destination,
        )
        return True

    def run(self, task_id: int) -> None:
        selected = self.repository.selected_items(task_id)
        if self.repository.should_stop(task_id):
            return
        pending = [item for item in selected if not self._restore_completed(item)]
        if not selected:
            self.repository.set_task_status(task_id, "failed", "没有选择视频")
            return
        if not pending:
            self.repository.set_task_status(task_id, "completed")
            return

        self.repository.set_task_status(task_id, "processing")
        for item in pending:
            self.repository.update_item(int(item["id"]), status="queued", progress=0)

        asr_config = self.asr_config or ASRConfig(
            model_path=self.config.model_path,
            device=Device(self.config.device),
            language=self.config.language,
            timestamps=self.config.timestamps,
            forced_aligner=self.config.forced_aligner,
            output_dir=self.config.output_dir,
            chunk_seconds=self.config.chunk_seconds,
            chunk_overlap_seconds=self.config.chunk_overlap_seconds,
        )
        session: object | None = None
        failures = 0
        futures: dict[Future[list[Path]], dict[str, Any]] = {}
        with ThreadPoolExecutor(
            max_workers=self.config.download_concurrency,
            thread_name_prefix="webvideo-download",
        ) as pool:
            for item in pending:
                futures[pool.submit(self._download_one, task_id, item)] = item

            for future in as_completed(futures):
                item = futures[future]
                item_id = int(item["id"])
                if self.repository.should_stop(task_id):
                    self.repository.update_item(item_id, status="cancelled")
                    for f in futures:
                        f.cancel()
                    break
                try:
                    files = future.result()
                except DownloadCancelled:
                    self.repository.update_item(item_id, status="cancelled")
                    self.downloader.cleanup(item_id)
                    continue
                except MediaDownloadError as exc:
                    failures += 1
                    self.repository.update_item(
                        item_id,
                        status="unsupported" if exc.unsupported else "failed",
                        error=str(exc),
                    )
                    self.downloader.cleanup(item_id)
                    continue
                except Exception as exc:
                    failures += 1
                    self.repository.update_item(item_id, status="failed", error=str(exc))
                    self.downloader.cleanup(item_id)
                    continue

                try:
                    if self.repository.should_stop(task_id):
                        self.repository.update_item(item_id, status="cancelled")
                        continue
                    if session is None:
                        session = self.session_factory(
                            asr_config.model_path, asr_config.device,
                            forced_aligner=asr_config.forced_aligner if asr_config.timestamps else None,
                        )
                    self.repository.update_item(
                        item_id, status="transcribing", progress=0
                    )
                    bodies: list[str] = []
                    languages: list[str] = []
                    for part_index, audio_path in enumerate(files):
                        if self.repository.should_stop(task_id):
                            self.repository.update_item(item_id, status="cancelled")
                            break
                        def on_progress(event: dict[str, Any]) -> None:
                            audio_index = int(event.get("audio_chunk_index") or 1)
                            audio_total = max(
                                1, int(event.get("audio_total_chunks") or 1)
                            )
                            if event.get("event") == "audio_chunk_started":
                                part_fraction = (audio_index - 1) / audio_total
                            elif event.get("event") == "chunk_started":
                                current = int(event.get("chunk_index") or 0)
                                total = max(1, int(event.get("total_chunks") or 1))
                                inner_fraction = current / total
                                part_fraction = (
                                    audio_index - 1 + inner_fraction
                                ) / audio_total
                            else:
                                return
                            overall = (part_index + part_fraction) / len(files) * 100
                            self.repository.update_item(
                                item_id,
                                status="transcribing",
                                progress=overall,
                            )

                        text, language = transcribe_audio_text(
                            session,
                            audio_path,
                            asr_config,
                            on_progress=on_progress,
                            should_stop=lambda: self.repository.should_stop(task_id),
                        )
                        if len(files) > 1:
                            text = f"【媒体部分 {part_index + 1}】\n{text.strip()}\n"
                        bodies.append(text.strip())
                        if language and language not in languages:
                            languages.append(language)
                    if self.repository.should_stop(task_id):
                        self.repository.update_item(item_id, status="cancelled")
                        continue
                    transcript = "\n\n".join(bodies).rstrip() + "\n"
                    language = ", ".join(languages)
                    record = self.repository.transcript_record(item_id)
                    if record is None:
                        raise RuntimeError("转录条目在数据库中丢失")
                    record["transcript_text"] = transcript
                    record["detected_language"] = language
                    destination = render_record(record, self.config.output_dir)
                    self.repository.store_transcript(
                        item_id, transcript, language, destination
                    )
                except Exception as exc:
                    failures += 1
                    self.repository.update_item(
                        item_id, status="failed", error=str(exc)
                    )
                finally:
                    self.downloader.cleanup(item_id)

        if self.repository.should_stop(task_id):
            for item in pending:
                self.downloader.cleanup(int(item["id"]))
            return
        elif failures:
            self.repository.set_task_status(task_id, "completed_with_errors")
        else:
            self.repository.set_task_status(task_id, "completed")
