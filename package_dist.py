# Author: wenhao
"""将 frontend/dist 目录压缩并输出到 scripts/dist.zip。"""

from __future__ import annotations

import os
import tempfile
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = PROJECT_ROOT / "frontend" / "dist"
TARGET_DIR = PROJECT_ROOT / "scripts"
ARCHIVE_NAME = "dist.zip"


def create_zip_archive(source_dir: Path, target_zip: Path) -> None:
    """将指定目录压缩为 zip 文件。

    参数:
        source_dir: 待压缩的源目录。
        target_zip: 目标 zip 文件路径。

    返回:
        None。
    """
    if not source_dir.exists():
        raise FileNotFoundError(f"未找到待压缩目录：{source_dir}")
    if not source_dir.is_dir():
        raise NotADirectoryError(f"待压缩路径不是目录：{source_dir}")

    with zipfile.ZipFile(target_zip, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file_path in source_dir.rglob("*"):
            if file_path.is_file():
                archive.write(file_path, file_path.relative_to(source_dir.parent))


def package_dist() -> Path:
    """压缩 frontend/dist 并覆盖 scripts/dist.zip。

    参数:
        无。

    返回:
        Path: 最终生成的 dist.zip 文件路径。
    """
    if not TARGET_DIR.is_dir():
        raise FileNotFoundError(f"未找到目标 scripts 目录：{TARGET_DIR}")

    final_archive = TARGET_DIR / ARCHIVE_NAME

    with tempfile.TemporaryDirectory(prefix="pcdn_tx_dist_", dir=TARGET_DIR) as temp_dir:
        temp_archive = Path(temp_dir) / ARCHIVE_NAME
        create_zip_archive(DIST_DIR, temp_archive)
        os.replace(temp_archive, final_archive)

    return final_archive


def main() -> None:
    """执行 dist 打包流程并打印输出路径。

    参数:
        无。

    返回:
        None。
    """
    archive_path = package_dist()
    print(f"已生成压缩包：{archive_path}")


if __name__ == "__main__":
    main()
