from __future__ import annotations

import shutil
from pathlib import Path

from .config import WebVideoConfig
from .repository import WebVideoRepository


class ArtifactDeleteError(RuntimeError):
    pass


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


class ArtifactCleaner:
    def __init__(
        self, config: WebVideoConfig, repository: WebVideoRepository
    ) -> None:
        self.config = config
        self.repository = repository

    def delete(self, item_id: int) -> None:
        record = self.repository.transcript_record(item_id)
        if record is None:
            raise KeyError(item_id)
        output_value = str(record.get("output_path") or "").strip()
        cache_value = str(record.get("audio_cache_path") or "").strip()
        output = Path(output_value) if output_value else None
        cache = Path(cache_value) if cache_value else None
        if output is not None and not _inside(output, self.config.output_dir):
            raise ArtifactDeleteError("转录文件不在 WebVideo 输出目录中")
        if cache is not None and not _inside(cache, self.config.cache_dir):
            raise ArtifactDeleteError("媒体缓存不在 WebVideo 缓存目录中")
        try:
            if output is not None:
                output.unlink(missing_ok=True)
            if cache is not None:
                if cache.is_dir():
                    shutil.rmtree(cache)
                else:
                    cache.unlink(missing_ok=True)
        except OSError as exc:
            raise ArtifactDeleteError(str(exc) or "删除视频数据失败") from exc
        self.repository.clear_item_artifacts(item_id)
