# -*- coding: utf-8 -*-
"""素材库索引的导出与导入：让一台机器建好的索引能被其他人直接用。

共享的是"哪句话出现在哪个主播的哪场直播的第几秒"，**不含视频文件本身**，
也不含任何本机绝对路径——用户拿着结果里的视频名称（一般带时间戳）自己去
网盘找文件即可。

跨机身份口径（见 ``material_library._video_identity``）：
    有内容指纹 -> ``cid:<指纹>``
    否则      -> ``主播 + 文件名 + 相对素材根目录的路径``
用"名字 + 相对路径"而不是只用文件名，是因为同一文件名可能出现在多个日期
目录下（同名不同场次），只看文件名会把不同场次误并成一个。
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from material_library import (
    SCHEMA_VERSION,
    MaterialIndex,
    _path_key,
    _video_identity,
)

ARCHIVE_FORMAT = "liveclipper-material-index"
ARCHIVE_FORMAT_VERSION = 1

MANIFEST_NAME = "manifest.json"
DATABASE_NAME = "material_index.sqlite3.gz"
README_NAME = "使用说明.txt"

ProgressFn = Callable[[str, str], None]


def _emit(progress: ProgressFn | None, message: str, level: str = "info") -> None:
    if progress is None:
        return
    try:
        progress(str(message), str(level))
    except TypeError:
        try:
            progress(str(message))  # type: ignore[call-arg]
        except Exception:
            pass
    except Exception:
        pass


def _read_app_version() -> str:
    try:
        version_file = Path(__file__).resolve().parent / "version.json"
        payload = json.loads(version_file.read_text(encoding="utf-8-sig"))
        return str(payload.get("version") or payload.get("latest_version") or "")
    except Exception:
        return ""


# --------------------------------------------------------------------------
# 导出
# --------------------------------------------------------------------------

def export_index(
    index: MaterialIndex,
    output_path: str,
    *,
    source_label: str = "",
    publishers: Sequence[str] | None = None,
    paths: Sequence[str] | None = None,
    lib_ids: Sequence[int] | None = None,
    include_unlabeled: bool = False,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """把索引导出成可分享的压缩包（剥离本机绝对路径）。

    三个筛选维度，任选其一（也可以都不给 = 全部）：
    - ``lib_ids``：按索引导出（一个素材目录 / 一个导入包 = 一个索引，推荐）；
    - ``publishers``：按主播分卷，一个主播一个 zip；
    - ``paths``：按目录分卷，一个文件夹一个 zip。

    只导出选中的部分；未标注主播要单独勾选（在列表里显示为"(未标注主播)"）。
    """
    started = time.time()
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="material_export_"))
    portable_db = work / "portable.sqlite3"
    selected = [str(item) for item in (publishers or []) if str(item).strip()]
    selected_paths = [str(item) for item in (paths or []) if str(item).strip()]
    selected_libs = [int(item) for item in (lib_ids or []) if int(item or 0) > 0]
    try:
        if selected_libs:
            names = {item["id"]: item["name"] for item in index.libraries()}
            scope_text = "、".join(names.get(item, f"索引{item}") for item in selected_libs[:3])
            if len(selected_libs) > 3:
                scope_text += f" 等 {len(selected_libs)} 个索引"
        elif selected_paths:
            scope_text = "、".join(Path(item).name for item in selected_paths[:3])
            if len(selected_paths) > 3:
                scope_text += f" 等 {len(selected_paths)} 个目录"
        elif selected:
            scope_text = "、".join(selected[:4]) + ("…" if len(selected) > 4 else "")
        else:
            scope_text = "全部主播"
        _emit(progress, f"正在整理索引（{scope_text}，剥离本机路径）…", "info")
        origin, stats = _build_portable_database(
            index, portable_db, source_label,
            publishers=selected, paths=selected_paths, lib_ids=selected_libs,
            include_unlabeled=include_unlabeled,
        )
        if stats["videos"] <= 0:
            raise ValueError(
                "筛选后没有可导出的内容："
                + ("请至少勾选一个索引、主播或目录。" if (selected or selected_paths or selected_libs)
                   else "素材库还是空的。")
            )
        _emit(
            progress,
            f"待导出：{stats['videos']} 个视频 / {stats['sentences']} 句 / "
            f"{stats['publishers']} 个主播",
            "info",
        )

        manifest = {
            "format": ARCHIVE_FORMAT,
            "format_version": ARCHIVE_FORMAT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "app_version": _read_app_version(),
            "created_at": time.time(),
            "source_label": origin.get("source_label", ""),
            "root_name": origin.get("root_name", ""),
            "scope": scope_text,
            "videos": stats["videos"],
            "sentences": stats["sentences"],
            "duration": stats["duration"],
            "publishers": stats["publisher_rows"],
            "with_content_id": stats["with_content_id"],
            "note": (
                "共享的是口播文案与时间点，不含视频文件；"
                "视频请按结果里的文件名（一般带时间戳）自行在网盘查找。"
            ),
        }

        _emit(progress, "正在压缩…", "info")
        database_gz = work / DATABASE_NAME
        with portable_db.open("rb") as source, gzip.open(database_gz, "wb", compresslevel=6) as sink:
            shutil.copyfileobj(source, sink, length=1024 * 1024)

        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.write(database_gz, DATABASE_NAME)
            archive.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
            archive.writestr(README_NAME, _readme_text(manifest))

        size = target.stat().st_size
        _emit(
            progress,
            f"导出完成：{target.name}（{size / 1024 / 1024:.1f} MB，"
            f"{stats['videos']} 个视频 / {stats['sentences']} 句 / 约 "
            f"{stats['duration'] / 3600:.1f} 小时），用时 {time.time() - started:.1f} 秒，可直接发给同事导入。",
            "success",
        )
        return {
            "ok": True,
            "path": str(target),
            "size_bytes": size,
            "videos": stats["videos"],
            "sentences": stats["sentences"],
            "publishers": stats["publishers"],
            "elapsed": round(time.time() - started, 2),
            "manifest": manifest,
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _build_portable_database(
    index: MaterialIndex,
    destination: Path,
    source_label: str,
    *,
    publishers: Sequence[str] | None = None,
    paths: Sequence[str] | None = None,
    lib_ids: Sequence[int] | None = None,
    include_unlabeled: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """复制一份索引，并把本机绝对路径替换成跨机身份。

    ``publishers`` / ``paths`` 任一非空时只保留命中部分（其余整条删除），
    这样每个索引包可以对准一个主播或一个目录分发。
    """
    with index._session() as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.commit()
    shutil.copy2(index.db_path, destination)

    connection = sqlite3.connect(str(destination))
    connection.row_factory = sqlite3.Row
    try:
        selected = [str(item) for item in (publishers or []) if str(item).strip()]
        wanted = set(selected)
        wanted_dirs = {str(item).strip().lower() for item in (paths or []) if str(item).strip()}
        wanted_libs = {int(item) for item in (lib_ids or []) if int(item or 0) > 0}
        rows = connection.execute(
            """
            SELECT id, path, name, publisher, dir, rel_path, origin_label, content_id, lib_id
            FROM videos ORDER BY id
            """
        ).fetchall()
        if wanted or wanted_dirs or wanted_libs:
            # 在 Python 侧判断归属：比把列表拼进 SQL 更直观，也避免绑定写法出岔子
            def keep(row: sqlite3.Row) -> bool:
                if wanted_libs:
                    return int(row["lib_id"] or 0) in wanted_libs
                if wanted_dirs:
                    return str(row["dir"] or "").strip().lower() in wanted_dirs
                publisher = str(row["publisher"] or "")
                return publisher in wanted or (include_unlabeled and not publisher)

            rows = [row for row in rows if keep(row)]
            kept_ids = {int(item["id"]) for item in rows}
            drop_ids = [
                int(item["id"]) for item in connection.execute("SELECT id FROM videos").fetchall()
                if int(item["id"]) not in kept_ids
            ]
            for start in range(0, len(drop_ids), 200):
                batch = drop_ids[start:start + 200]
                placeholders = ",".join("?" for _ in batch)
                sentence_ids = [
                    int(item["id"]) for item in connection.execute(
                        f"SELECT id FROM sentences WHERE video_id IN ({placeholders})", batch
                    ).fetchall()
                ]
                for index_ in range(0, len(sentence_ids), 800):
                    chunk = sentence_ids[index_:index_ + 800]
                    marks = ",".join("?" for _ in chunk)
                    connection.execute(
                        f"DELETE FROM sentence_grams WHERE sentence_id IN ({marks})", chunk
                    )
                connection.execute(
                    f"DELETE FROM sentences WHERE video_id IN ({placeholders})", batch
                )
                connection.execute(f"DELETE FROM videos WHERE id IN ({placeholders})", batch)
            connection.commit()
        if not rows:
            raise ValueError("筛选后没有可导出的视频。")

        roots = [str(item["path"]) for item in connection.execute(
            "SELECT path FROM material_roots ORDER BY length(path) DESC"
        ).fetchall()]
        root_name = Path(roots[0]).name if roots else ""

        # 第一遍：确定每个视频相对哪个素材根目录，并回填缺失的 rel_path
        resolved: dict[int, str] = {}
        labels: dict[int, str] = {}
        from material_library import _common_parent, _label_from_rel, _relative_to_root

        fallback_root = _common_parent([str(row["path"]) for row in rows])
        for row in rows:
            rel = str(row["rel_path"] or "")
            label = str(row["origin_label"] or "")
            if not rel:
                for root in roots:
                    guess = _relative_to_root(str(row["path"]), root)
                    if guess:
                        rel = guess
                        label = label or _label_from_rel(guess) or Path(root).name
                        break
            if not rel and fallback_root:
                guess = _relative_to_root(str(row["path"]), fallback_root)
                if guess:
                    rel = guess
                    label = label or _label_from_rel(guess) or Path(fallback_root).name
            resolved[int(row["id"])] = rel
            labels[int(row["id"])] = label

        # 第二遍：重名且都没有 rel_path 的，补上父目录名，避免导入时被误并
        seen: dict[tuple[str, str], list[int]] = {}
        for row in rows:
            key = (str(row["publisher"] or ""), str(row["name"] or ""))
            seen.setdefault(key, []).append(int(row["id"]))
        for (_publisher, _name), ids in seen.items():
            if len(ids) < 2:
                continue
            for video_id in ids:
                if resolved.get(video_id):
                    continue
                parent = ""
                for row in rows:
                    if int(row["id"]) == video_id:
                        parent = Path(str(row["path"])).parent.name
                        break
                resolved[video_id] = parent or ""

        # 第三遍：写入跨机身份，并抹掉本机路径。
        # path 列有 UNIQUE 约束，不能都清成空串（空串之间也算重复），
        # 所以投影成唯一的共享标识路径。
        for row in rows:
            video_id = int(row["id"])
            rel = resolved.get(video_id, "")
            label = labels.get(video_id, "") or root_name
            connection.execute(
                "UPDATE videos SET rel_path=?, origin_label=? WHERE id=?",
                (rel, label, video_id),
            )
            connection.execute(
                "UPDATE videos SET path=?, path_key='', dir='', dir_key='', mtime=0 WHERE id=?",
                (f"shared/{video_id}", video_id),
            )
        connection.execute("DROP TABLE IF EXISTS search_history")
        # 关键：筛选删过视频/句子后，全文索引里还留着指向已删句子的旧行，
        # 不重建的话导入方检索会命中"幽灵"结果。
        try:
            connection.execute(
                "INSERT INTO sent_seg_fts(sent_seg_fts) VALUES('rebuild')"
            )
        except sqlite3.Error:
            pass
        connection.commit()

        stats_row = connection.execute(
            "SELECT COUNT(*) AS videos, COALESCE(SUM(sentence_count),0) AS sentences, "
            "COALESCE(SUM(duration),0) AS duration, "
            "SUM(CASE WHEN content_id<>'' THEN 1 ELSE 0 END) AS with_cid FROM videos"
        ).fetchone()
        publisher_rows = [
            {"publisher": str(item["publisher"] or ""), "videos": int(item["videos"] or 0)}
            for item in connection.execute(
                "SELECT publisher, COUNT(*) AS videos FROM videos "
                "GROUP BY publisher ORDER BY videos DESC"
            ).fetchall()
        ]
        connection.execute("VACUUM")
        connection.commit()
        stats = {
            "videos": int(stats_row["videos"] or 0),
            "sentences": int(stats_row["sentences"] or 0),
            "duration": float(stats_row["duration"] or 0.0),
            "with_content_id": int(stats_row["with_cid"] or 0),
            "publishers": len(publisher_rows),
            "publisher_rows": publisher_rows,
        }
        return {"source_label": source_label or root_name, "root_name": root_name}, stats
    finally:
        connection.close()


def _readme_text(manifest: Mapping[str, Any]) -> str:
    publishers = "、".join(
        f"{item.get('publisher') or '(未标注)'}({item.get('videos')})"
        for item in (manifest.get("publishers") or [])[:12]
    )
    return (
        "LiveClipper 素材库索引包\n"
        "========================\n\n"
        f"导出时间：{time.strftime('%Y-%m-%d %H:%M', time.localtime(manifest.get('created_at') or time.time()))}\n"
        f"来源：{manifest.get('source_label') or '-'}\n"
        f"内容：{manifest.get('videos')} 个视频 / {manifest.get('sentences')} 句"
        f" / 共 {float(manifest.get('duration') or 0) / 3600:.1f} 小时\n"
        f"主播：{publishers or '-'}\n\n"
        "怎么用：\n"
        "1. 打开 LiveClipper → 左侧「素材库」；\n"
        "2. 点「导入索引」，选中这个 zip 包；\n"
        "3. 导入后直接检索即可，结果会显示主播、视频文件名和命中时间点；\n"
        "4. 想直接跳转播放，需要本地有同名视频（放在自己的素材目录里）；\n"
        "   没有的话，拿结果里的视频文件名去网盘查找下载即可。\n\n"
        "说明：这个包里只有文案索引和时间点，不含视频文件本身，也不需要本机路径。\n"
    )


# --------------------------------------------------------------------------
# 导入
# --------------------------------------------------------------------------

def inspect_archive(archive_path: str) -> dict[str, Any]:
    """只读压缩包信息，供导入前确认。"""
    path = Path(archive_path)
    if not path.is_file():
        raise FileNotFoundError(f"找不到索引包：{archive_path}")
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        if MANIFEST_NAME not in names:
            raise ValueError("这个压缩包里没有 manifest.json，可能不是素材库索引包。")
        manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
        if manifest.get("format") != ARCHIVE_FORMAT:
            raise ValueError(f"索引包格式不认识：{manifest.get('format')}")
        has_db = DATABASE_NAME in names
    manifest = dict(manifest)
    manifest["ok"] = True
    manifest["has_database"] = has_db
    manifest["archive_size"] = path.stat().st_size
    return manifest


def import_index(
    index: MaterialIndex,
    archive_path: str,
    *,
    progress: ProgressFn | None = None,
    cancel_event: Any = None,
) -> dict[str, Any]:
    """把索引包合并进本地素材库（按跨机身份去重）。"""
    started = time.time()
    path = Path(archive_path)
    manifest = inspect_archive(str(path))
    if not manifest.get("has_database"):
        raise ValueError("索引包里缺少数据库文件。")
    if int(manifest.get("schema_version") or 0) != SCHEMA_VERSION:
        _emit(
            progress,
            f"提示：索引包结构版本 {manifest.get('schema_version')} 与当前 "
            f"{SCHEMA_VERSION} 不一致，尝试按兼容方式导入。",
            "warning",
        )

    work = Path(tempfile.mkdtemp(prefix="material_import_"))
    source_db = work / "incoming.sqlite3"
    try:
        _emit(progress, "正在解包…", "info")
        with zipfile.ZipFile(path) as archive:
            with archive.open(DATABASE_NAME) as raw, gzip.open(raw, "rb") as unzipped, source_db.open("wb") as sink:
                shutil.copyfileobj(unzipped, sink, length=1024 * 1024)

        source = sqlite3.connect(str(source_db))
        source.row_factory = sqlite3.Row
        try:
            rows = source.execute(
                """
                SELECT id, name, publisher, rel_path, origin_label, content_id,
                       duration, session_epoch, asr_provider, has_word_timing, sentence_count
                FROM videos ORDER BY id
                """
            ).fetchall()
            known = index.known_identities()
            _emit(progress, f"索引包里有 {len(rows)} 个视频，开始合并…", "info")

            # 导入 = 在这个库里新建一个**独立索引**，跟本机自己建的索引并列，
            # 用户可以单独切到它来搜、单独导出、单独删除。
            scope_label = str(manifest.get("source_label") or "").strip()
            lib_name = scope_label or Path(str(path)).stem
            new_lib_id = index.create_library(
                name=lib_name,
                kind="imported",
                source=str(path),
                origin_label=scope_label or lib_name,
                note=f"导入自 {Path(str(path)).name}",
            )
            _emit(progress, f"正在建立索引「{lib_name}」…", "info")

            added = skipped = matched_local = 0
            sentences_added = 0
            touched_publishers: set[str] = set()
            # 批量通道：先灌数据，最后一次性重建 FTS 与热门文案
            index.begin_bulk_import()
            with index._session() as connection:
                for position, row in enumerate(rows, 1):
                    if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
                        _emit(progress, "已中断导入。", "warning")
                        break
                    name = str(row["name"] or "")
                    publisher = str(row["publisher"] or "")
                    rel = str(row["rel_path"] or "")
                    origin_label = str(row["origin_label"] or "")
                    content_id = str(row["content_id"] or "")
                    identity = _video_identity(name, publisher, rel, content_id)
                    if identity in known:
                        skipped += 1
                        continue

                    cues = [
                        {"start": float(item["start"]), "end": float(item["end"]),
                         "text": str(item["raw_text"])}
                        for item in source.execute(
                            "SELECT start, end, raw_text FROM sentences WHERE video_id=? ORDER BY seq",
                            (int(row["id"]),),
                        ).fetchall()
                    ]
                    if not cues:
                        skipped += 1
                        continue

                    existing = index._find_video_row(connection, name, publisher)
                    if existing is not None:
                        matched_local += 1

                    info = index.bulk_insert_video(
                        connection,
                        name=name,
                        publisher=publisher,
                        sentences=cues,
                        duration=float(row["duration"] or 0.0),
                        session_epoch=float(row["session_epoch"] or 0.0),
                        asr_provider=str(row["asr_provider"] or "imported"),
                        has_word_timing=bool(row["has_word_timing"]),
                        rel_path=rel,
                        origin_label=origin_label or lib_name,
                        content_id=content_id,
                        lib_id=new_lib_id,
                        # 本机没有这个文件时，路径用一个带来源的占位标识，
                        # 结果里仍显示真实视频名，用户照着去网盘找即可。
                        placeholder_path=str(Path("index") / (origin_label or "shared") / rel / name),
                    )
                    sentences_added += int(info.get("sentences") or 0)
                    known.add(identity)
                    added += 1
                    touched_publishers.add(publisher)
                    if position % 50 == 0:
                        _emit(
                            progress,
                            f"合并中 {position}/{len(rows)}（新增 {added}，跳过 {skipped}）…",
                            "info",
                        )

            if added:
                _emit(progress, "正在重建检索索引与热门文案…", "info")
                index.finish_bulk_import()
                try:
                    with index._session() as connection:
                        index._refresh_library_stats(connection, new_lib_id)
                except Exception:
                    pass
            else:
                # 一个视频都没进来（全是重复）：别留一个空的索引条目
                try:
                    index.remove_library(new_lib_id)
                except Exception:
                    pass
        finally:
            source.close()

        message = (
            f"导入完成：新增 {added} 个视频 / {sentences_added} 句，"
            f"跳过重复 {skipped} 个"
        )
        if matched_local:
            message += f"，其中 {matched_local} 个本地已有同名视频（可直接跳转播放）"
        _emit(progress, message + "。", "success")
        # 记一笔导入历史：界面上要能看出这个库装了哪几批索引
        try:
            index.record_import(
                file_name=path.name,
                source=str(manifest.get("source_label") or ""),
                videos=added,
                sentences=sentences_added,
                note=str(manifest.get("scope") or ""),
            )
        except Exception:
            pass
        return {
            "ok": True,
            "added": added,
            "skipped": skipped,
            "matched_local": matched_local,
            "sentences": sentences_added,
            "publishers": sorted(item for item in touched_publishers if item),
            "source_label": manifest.get("source_label", ""),
            "elapsed": round(time.time() - started, 2),
            "message": message,
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


def default_export_name(source_label: str = "", publishers: Sequence[str] | None = None) -> str:
    """导出文件名：素材库索引_<目录或主播>_<日期>.zip"""
    names = [str(item).strip() for item in (publishers or []) if str(item).strip()]
    if names:
        base = names[0] if len(names) == 1 else f"{len(names)}个主播"
    else:
        # 按目录导出时 source_label 已经是"影子"这种短标签，直接用
        base = str(source_label or "全部").split("、")[0].strip()
    safe = "".join(ch for ch in base if ch not in '\\/:*?"<>|').strip().replace(" ", "")
    stamp = time.strftime("%Y%m%d")
    return f"素材库索引_{safe[:24] or '共享'}_{stamp}.zip"
