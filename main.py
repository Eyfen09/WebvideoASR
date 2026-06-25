from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="videoASR")
    subparsers = parser.add_subparsers(dest="mode", required=True)

    local_parser = subparsers.add_parser("local", help="转写本地音频文件或目录")
    local_parser.add_argument("input_path", type=Path, help="音频文件或目录")
    local_device = local_parser.add_mutually_exclusive_group()
    local_device.add_argument("--gpu", action="store_const", const=False, dest="cpu", default=False, help="使用 GPU 推理（默认）")
    local_device.add_argument("--cpu", action="store_const", const=True, dest="cpu", help="使用 CPU 推理")

    bilibili_parser = subparsers.add_parser(
        "bilibili", help="扫码登录后转写 Bilibili 视频或收藏夹"
    )
    bilibili_parser.add_argument("url", help="视频链接或带 fid 的收藏夹链接")

    webvideo_parser = subparsers.add_parser(
        "webvideo", help="启动通用网络视频解析与转录页面"
    )
    webvideo_device = webvideo_parser.add_mutually_exclusive_group()
    webvideo_device.add_argument("--gpu", action="store_const", const=False, dest="cpu", default=False, help="使用 GPU 推理（默认）")
    webvideo_device.add_argument("--cpu", action="store_const", const=True, dest="cpu", help="使用 CPU 推理")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.mode == "local":
            from local.cli import run

            return run(args.input_path, cpu=args.cpu)
        if args.mode == "bilibili":
            from bilibili.cli import run

            return run(args.url)
        from webvideo.cli import run

        return run(cpu=args.cpu)
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
