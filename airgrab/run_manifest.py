"""Shared pipeline manifest: one summary + batches list (no per-run entries)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

RUN_DIR_KEYS = ("input_dir", "output_dir", "error_dir")

_BATCH_COUNT_SUFFIX = "_count"
_BATCH_SUMMARY_EXCLUDE = frozenset(
    {
        "success_count",
        "error_count",
        "restaurant_count",
    }
)


def _empty_manifest() -> dict[str, Any]:
    return {
        "input_dir": None,
        "output_dir": None,
        "error_dir": None,
        "started_at": None,
        "finished_at": None,
        "batches": [],
    }


def _normalize_dir_path(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return str(Path(text).resolve())
    except OSError:
        return text


def _run_dir_map(record: dict[str, Any]) -> dict[str, str | None]:
    return {key: _normalize_dir_path(record.get(key)) for key in RUN_DIR_KEYS}


def _batch_count_keys(batches: list[dict[str, Any]]) -> list[str]:
    keys: set[str] = set()
    for batch in batches:
        if not isinstance(batch, dict):
            continue
        for key, value in batch.items():
            if key in _BATCH_SUMMARY_EXCLUDE:
                continue
            if key.endswith(_BATCH_COUNT_SUFFIX) and isinstance(value, (int, float)):
                keys.add(key)
    return sorted(keys)


def _earliest_timestamp(*values: Any) -> str | None:
    timestamps = [str(value) for value in values if value]
    return min(timestamps) if timestamps else None


def _latest_timestamp(*values: Any) -> str | None:
    timestamps = [str(value) for value in values if value]
    return max(timestamps) if timestamps else None


def recompute_summary(manifest: dict[str, Any]) -> None:
    batches = manifest.get("batches")
    if not isinstance(batches, list):
        batches = []
        manifest["batches"] = batches

    preserved = {
        key: manifest[key]
        for key in (*RUN_DIR_KEYS, "started_at", "finished_at")
        if key in manifest
    }
    manifest.clear()
    manifest.update(preserved)
    manifest["batches"] = batches

    manifest["total_batches_count"] = len(batches)
    manifest["successful_batches_count"] = sum(
        1 for batch in batches if batch.get("status") == "success"
    )
    manifest["error_batches_count"] = (
        manifest["total_batches_count"] - manifest["successful_batches_count"]
    )
    for key in _batch_count_keys(batches):
        manifest[key] = sum(int(batch.get(key) or 0) for batch in batches)


def same_run_dirs(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_dirs = _run_dir_map(left)
    right_dirs = _run_dir_map(right)
    if not all(left_dirs.values()) or not all(right_dirs.values()):
        return False
    return left_dirs == right_dirs


def merge_batches(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = list(existing)
    index_by_id = {
        batch.get("batch_id"): index
        for index, batch in enumerate(merged)
        if batch.get("batch_id")
    }

    for batch in incoming:
        batch_id = batch.get("batch_id")
        if batch_id in index_by_id:
            merged[index_by_id[batch_id]] = batch
        else:
            if batch_id:
                index_by_id[batch_id] = len(merged)
            merged.append(batch)

    return merged


def _consolidate_legacy_runs(runs: list[Any]) -> dict[str, Any]:
    manifest = _empty_manifest()
    all_batches: list[dict[str, Any]] = []
    started_values: list[Any] = []
    finished_values: list[Any] = []
    dirs_source: dict[str, Any] | None = None

    for run in runs:
        if not isinstance(run, dict):
            continue
        batches = run.get("batches")
        if isinstance(batches, list):
            all_batches = merge_batches(all_batches, batches)
        if run.get("started_at"):
            started_values.append(run["started_at"])
        if run.get("finished_at"):
            finished_values.append(run["finished_at"])
        if dirs_source is None and all(_run_dir_map(run).values()):
            dirs_source = run

    if dirs_source is None and runs:
        last = runs[-1]
        dirs_source = last if isinstance(last, dict) else None

    if dirs_source:
        for key in RUN_DIR_KEYS:
            manifest[key] = dirs_source.get(key)

    manifest["started_at"] = _earliest_timestamp(*started_values)
    manifest["finished_at"] = _latest_timestamp(*finished_values)
    manifest["batches"] = all_batches
    recompute_summary(manifest)
    return manifest


def normalize_manifest(data: dict[str, Any]) -> dict[str, Any]:
    if isinstance(data.get("runs"), list):
        return _consolidate_legacy_runs(data["runs"])

    manifest = _empty_manifest()
    for key in RUN_DIR_KEYS:
        if key in data:
            manifest[key] = data[key]
    if "started_at" in data:
        manifest["started_at"] = data["started_at"]
    if "finished_at" in data:
        manifest["finished_at"] = data["finished_at"]

    batches = data.get("batches")
    manifest["batches"] = list(batches) if isinstance(batches, list) else []
    recompute_summary(manifest)
    return manifest


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return _empty_manifest()
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return _empty_manifest()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _empty_manifest()
    if not isinstance(data, dict):
        return _empty_manifest()
    return normalize_manifest(data)


def _ordered_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    ordered: dict[str, Any] = {}
    for key in (*RUN_DIR_KEYS, "started_at", "finished_at"):
        if key in manifest:
            ordered[key] = manifest[key]
    for key in sorted(manifest.keys()):
        if key.endswith(_BATCH_COUNT_SUFFIX) and key not in ordered:
            ordered[key] = manifest[key]
    ordered["batches"] = manifest.get("batches", [])
    return ordered


def save_manifest(path: Path, manifest: dict[str, Any]) -> None:
    normalized = normalize_manifest(manifest)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(_ordered_manifest(normalized), f, indent=2, ensure_ascii=False)
        f.write("\n")


def append_run_to_manifest(manifest: dict[str, Any], run_record: dict[str, Any]) -> None:
    """Merge new batch results into the single manifest summary."""
    incoming_batches = run_record.get("batches")
    if not isinstance(incoming_batches, list):
        incoming_batches = []

    has_existing = bool(manifest.get("batches")) or all(
        _normalize_dir_path(manifest.get(key)) for key in RUN_DIR_KEYS
    )

    if not has_existing:
        for key in RUN_DIR_KEYS:
            manifest[key] = run_record.get(key)
        manifest["started_at"] = run_record.get("started_at")
        manifest["finished_at"] = run_record.get("finished_at")
        manifest["batches"] = list(incoming_batches)
        recompute_summary(manifest)
        return

    if not same_run_dirs(manifest, run_record):
        for key in RUN_DIR_KEYS:
            manifest[key] = run_record.get(key)
        manifest["started_at"] = run_record.get("started_at")
        manifest["finished_at"] = run_record.get("finished_at")
        manifest["batches"] = list(incoming_batches)
        recompute_summary(manifest)
        return

    existing_batches = manifest.get("batches")
    if not isinstance(existing_batches, list):
        existing_batches = []
    manifest["batches"] = merge_batches(existing_batches, incoming_batches)
    manifest["started_at"] = _earliest_timestamp(
        manifest.get("started_at"),
        run_record.get("started_at"),
    )
    manifest["finished_at"] = _latest_timestamp(
        manifest.get("finished_at"),
        run_record.get("finished_at"),
    )
    for key in RUN_DIR_KEYS:
        manifest[key] = run_record.get(key)
    recompute_summary(manifest)
