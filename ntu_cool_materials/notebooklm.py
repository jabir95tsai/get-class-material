"""Coordinate resumable uploads using the planned sources and per-course journal."""
from __future__ import annotations

import shutil
import tempfile
from dataclasses import replace
from pathlib import Path

from .i18n import t
from .notebooklm_journal import STATE_NAME, import_lock, read_state, save_state
from .notebooklm_models import (
    ANNOUNCEMENT_PATHS, ImportPlan, ImportResult, NotebookAdapter, NotebookLMError,
    Source, is_announcement_title as is_announcement_title,
    strip_legacy_hash as strip_legacy_hash, validate_notebook_url,
)
from .notebooklm_plan import (
    DOCUMENT_TYPES as DOCUMENT_TYPES,
    MAX_FILE_BYTES as MAX_FILE_BYTES,
    MEDIA_TYPES as MEDIA_TYPES,
    build_import_plan as build_import_plan,
)
from .spinner import spinner
from .storage import sha256_file


def _superseded_announcements(record: dict, source: Source) -> dict[str, str]:
    if source.relative_path not in ANNOUNCEMENT_PATHS:
        return {}
    return {
        digest: entry["source_id"] for digest, entry in record.items()
        if entry.get("announcement_path") == source.relative_path and digest != source.digest
        and entry.get("status") == "complete" and entry.get("source_id")
    }


def reconcile_legacy_sources(plan, record, ready, adapter):
    """Bind completed legacy entries only to an unclaimed, byte-identical original."""
    by_title = {}
    for source_id, title in ready.items():
        by_title.setdefault(title, []).append(source_id)
    claimed = {entry.get("source_id") for entry in record.values()}
    repaired = 0
    for source in plan.sources:
        entry = record.get(source.digest, {})
        if entry.get("status") != "complete" or entry.get("source_id"):
            continue
        candidates = by_title.get(entry.get("title"), [])
        if len(candidates) != 1 or candidates[0] in claimed:
            continue
        source_id = candidates[0]
        if adapter.matches_original(source_id, source):
            entry["source_id"] = source_id
            claimed.add(source_id)
            repaired += 1
    return repaired


def import_plan(plan: ImportPlan, adapter: NotebookAdapter, *, notebook_url: str | None = None,
                max_sources: int = 50) -> ImportResult:
    if max_sources < 1:
        raise NotebookLMError(t("來源上限必須大於零。", "The source limit must be greater than zero."))
    if not plan.sources:
        return ImportResult()
    if notebook_url:
        notebook_url = validate_notebook_url(notebook_url)
    with import_lock(plan.root):
        path = plan.root / STATE_NAME
        state = read_state(path)
        target = notebook_url or state.get("default_url")
        url = validate_notebook_url(adapter.open_notebook(target, plan.root.name))
        state["default_url"] = url
        record = state["notebooks"].setdefault(url, {"sources": {}})["sources"]
        save_state(path, state)  # Save the notebook before starting any upload.
        ready = adapter.ready_sources()
        repaired = reconcile_legacy_sources(plan, record, ready, adapter)
        if repaired:
            save_state(path, state)
            print(t(f"已核對原檔並補回 {repaired} 個來源 ID。", f"Verified originals and restored {repaired} source IDs."))
        used_titles = set(ready.values())
        result = ImportResult(notebook_url=url)
        pending: list[Source] = []
        confirmed_announcements: list[Source] = []

        def remove_superseded(source: Source) -> None:
            obsolete = _superseded_announcements(record, source)
            if obsolete:
                kept_id = record[source.digest]["source_id"]
                adapter.delete_sources(set(obsolete.values()) - {kept_id}, keep_source_id=kept_id)
                for digest in obsolete:
                    del record[digest]
                save_state(path, state)

        for source in plan.sources:
            previous = record.get(source.digest, {})
            source_id = previous.get("source_id")
            if source_id in ready and (previous["status"] == "complete" or ready[source_id] == previous["title"]):
                previous.update(title=ready[source_id], status="complete")
                if previous.get("announcement_path") == source.relative_path:
                    confirmed_announcements.append(source)
                result.unchanged += 1
                continue
            if previous.get("status") == "pending":
                raise NotebookLMError(t(
                    f"先前上傳結果不明：{source.relative_path}。請在筆記本確認來源是否處理完成；"
                    "不會自動重送。若已確定不存在，才刪除紀錄中對應的 pending 項目後重試。",
                    f"An earlier upload of {source.relative_path} has an unknown result. Check the notebook; "
                    "it won't be re-sent automatically. Only if it's definitely missing, delete its pending "
                    "entry from the record and retry."))
            if not source_id and previous.get("status") == "complete" and previous["title"] in ready.values():
                result.unverified += 1
                continue
            title = source.title
            stem, suffix = Path(title).stem, Path(title).suffix
            counter = 2
            while title in used_titles:
                title = f"{stem} ({counter}){suffix}"
                counter += 1
            used_titles.add(title)
            pending.append(replace(source, title=title))
        save_state(path, state)
        for source in confirmed_announcements:
            remove_superseded(source)
        current_count = adapter.source_count()
        print(t(f"NotebookLM 來源：目前 {current_count}/{max_sources}，待上傳 {len(pending)}。",
                f"NotebookLM sources: {current_count}/{max_sources} now, {len(pending)} pending upload."))
        pending.sort(key=lambda source: not _superseded_announcements(record, source))
        remaining_ready_ids = set(ready)
        projected_count = current_count
        for source in pending:
            if projected_count >= max_sources:
                raise NotebookLMError(t("來源數將超過設定上限。須有足夠空位先上傳新版，才能移除舊公告。",
                                        "This would exceed the source limit. Free space to upload new sources before old announcements are removed."))
            retiring = remaining_ready_ids.intersection(_superseded_announcements(record, source).values())
            projected_count += 1 - len(retiring)
            remaining_ready_ids.difference_update(retiring)
        for source in pending:
            # Verify the staged copy against the planned digest before recording the upload.
            with tempfile.TemporaryDirectory(prefix=".notebooklm-upload-", dir=plan.root) as tmp:
                staged_name = source.title if Path(source.title).suffix else f"{source.title}{source.path.suffix}"
                staged = Path(tmp) / staged_name
                shutil.copyfile(source.path, staged)
                if sha256_file(staged) != (source.content_digest or source.digest):
                    raise NotebookLMError(t(f"檔案已變動，請重新執行：{source.relative_path}",
                                            f"File changed; run again: {source.relative_path}"))
                entry = {"title": source.title, "status": "pending"}
                if source.relative_path in ANNOUNCEMENT_PATHS:
                    entry["announcement_path"] = source.relative_path
                record[source.digest] = entry
                save_state(path, state)

                def remember_source_id(source_id: str) -> None:
                    entry["source_id"] = source_id
                    save_state(path, state)

                try:
                    with spinner(t(f"  上傳中 {source.relative_path}", f"  Uploading {source.relative_path}")):
                        adapter.upload(staged, source.title, on_created=remember_source_id)
                except Exception as exc:
                    # Do not include transport exception text: it may contain session URLs.
                    raise NotebookLMError(t(f"上傳尚未確認完成：{source.relative_path}。"
                                            "已保留 pending 紀錄，請檢查 NotebookLM 後再執行。",
                                            f"Upload not confirmed: {source.relative_path}. The pending "
                                            "record was kept; check NotebookLM, then run again.")) from exc
                entry["status"] = "complete"
                save_state(path, state)
                if "announcement_path" in entry:
                    remove_superseded(source)
                result.uploaded += 1
                print(f"  ✓ {source.relative_path}")
        return result

