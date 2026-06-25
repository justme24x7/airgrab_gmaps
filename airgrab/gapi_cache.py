"""Sharded on-disk cache for GAPI responses and processed results."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_PROVIDERS_PER_SHARD = 100
INDEX_FILENAME = "index.json"
SHARD_PREFIX = "shard_"
SHARD_PATTERN = re.compile(r"^shard_(\d+)\.json$")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def is_manually_verified_result(result: Any) -> bool:
    return isinstance(result, dict) and result.get("is_manually_verified") is True


def is_manually_verified_entry(entry: dict[str, Any]) -> bool:
    return is_manually_verified_result(entry.get("result"))


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _read_json_dict(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


class GapiCache:
    """Provider-id keyed cache split across shard JSON files."""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.index: dict[str, str] = {}
        self._shards: dict[str, dict[str, dict[str, Any]]] = {}
        self._dirty_shards: set[str] = set()
        self._index_dirty = False
        self._load_index()

    def _index_path(self) -> Path:
        return self.cache_dir / INDEX_FILENAME

    def _shard_path(self, shard_name: str) -> Path:
        return self.cache_dir / shard_name

    def _load_index(self) -> None:
        data = _read_json_dict(self._index_path())
        self.index = {
            str(provider_id): str(shard_name)
            for provider_id, shard_name in data.items()
            if provider_id and shard_name
        }

    def _save_index(self) -> None:
        _write_json(self._index_path(), self.index)

    def _load_shard(self, shard_name: str) -> dict[str, dict[str, Any]]:
        if shard_name in self._shards:
            return self._shards[shard_name]

        data = _read_json_dict(self._shard_path(shard_name))
        shard = {
            str(provider_id): entry
            for provider_id, entry in data.items()
            if isinstance(entry, dict)
        }
        self._shards[shard_name] = shard
        return shard

    def _save_shard(self, shard_name: str) -> None:
        shard = self._shards.get(shard_name, {})
        _write_json(self._shard_path(shard_name), shard)

    def _shard_numbers(self) -> list[int]:
        numbers: list[int] = []
        for name in (
            *self.cache_dir.glob(f"{SHARD_PREFIX}*.json"),
            *(Path(name) for name in self._shards),
            *(Path(name) for name in self.index.values()),
        ):
            match = SHARD_PATTERN.match(name.name)
            if match:
                numbers.append(int(match.group(1)))
        return numbers

    def _next_shard_name(self) -> str:
        numbers = self._shard_numbers()
        highest = max(numbers) if numbers else 0
        return f"{SHARD_PREFIX}{highest + 1:04d}.json"

    def _find_shard_with_room(self) -> str:
        shard_names = sorted(set(self.index.values()) | set(self._shards))
        for shard_name in shard_names:
            if len(self._load_shard(shard_name)) < MAX_PROVIDERS_PER_SHARD:
                return shard_name

        shard_name = self._next_shard_name()
        self._shards[shard_name] = {}
        return shard_name

    def prefetch(self, provider_ids: list[str]) -> None:
        shard_names = {
            self.index[provider_id]
            for provider_id in provider_ids
            if provider_id in self.index
        }
        for shard_name in shard_names:
            self._load_shard(shard_name)

    def get_entry(self, provider_id: str) -> dict[str, Any] | None:
        provider_id = provider_id.strip()
        if not provider_id:
            return None
        shard_name = self.index.get(provider_id)
        if not shard_name:
            return None
        shard = self._load_shard(shard_name)
        entry = shard.get(provider_id)
        return deepcopy(entry) if isinstance(entry, dict) else None

    def should_skip_gapi(self, provider_id: str) -> bool:
        entry = self.get_entry(provider_id)
        if not entry or not entry.get("is_gapi_called"):
            return False
        gapi_response = entry.get("gapi_response")
        return isinstance(gapi_response, dict)

    def record_gapi_call(
        self,
        provider: dict[str, Any],
        gapi_response: dict[str, Any],
        *,
        called_at: str | None = None,
    ) -> None:
        provider_id = str(provider.get("id") or "").strip()
        if not provider_id:
            return

        existing = self.get_entry(provider_id)
        entry: dict[str, Any] = {
            "id": provider_id,
            "local_id": provider.get("local_id", provider.get("localid")),
            "is_gapi_called": True,
            "gapi_called_at": called_at or utc_now_iso(),
            "gapi_response": deepcopy(gapi_response),
            "result": existing.get("result") if existing else None,
        }
        self._upsert_entry(provider_id, entry)

    def upsert_from_processed_record(self, record: dict[str, Any]) -> None:
        provider_id = str(record.get("id") or "").strip()
        if not provider_id:
            return

        existing = self.get_entry(provider_id)
        if existing and is_manually_verified_entry(existing):
            return

        results = record.get("results")
        result: dict[str, Any] | None = None
        if isinstance(results, dict):
            result = deepcopy(results)
            if result.get("is_manually_verified") is not True:
                result["is_manually_verified"] = False

        entry: dict[str, Any] = {
            "id": provider_id,
            "local_id": record.get("local_id", record.get("localid")),
            "is_gapi_called": True,
            "gapi_called_at": (existing.get("gapi_called_at") if existing else None)
            or utc_now_iso(),
            "gapi_response": deepcopy(record.get("gapi_response"))
            if isinstance(record.get("gapi_response"), dict)
            else existing.get("gapi_response")
            if existing
            else None,
            "result": result,
        }
        self._upsert_entry(provider_id, entry)

    def _upsert_entry(self, provider_id: str, entry: dict[str, Any]) -> None:
        shard_name = self.index.get(provider_id)
        if shard_name is None:
            shard_name = self._find_shard_with_room()
            self.index[provider_id] = shard_name
            self._index_dirty = True

        shard = self._load_shard(shard_name)
        shard[provider_id] = entry
        self._dirty_shards.add(shard_name)

    def import_entry(self, provider_id: str, entry: dict[str, Any]) -> None:
        """Insert or replace a cache entry (for one-off imports/migrations)."""
        provider_id = provider_id.strip()
        if not provider_id:
            return
        self._upsert_entry(provider_id, entry)

    def import_entries_bulk(self, entries: dict[str, dict[str, Any]]) -> int:
        """Import many entries, allocating a fresh shard for every 100 providers."""
        shard_files = 0
        items = list(entries.items())
        for offset in range(0, len(items), MAX_PROVIDERS_PER_SHARD):
            chunk = items[offset : offset + MAX_PROVIDERS_PER_SHARD]
            shard_name = self._next_shard_name()
            shard: dict[str, dict[str, Any]] = {}
            for provider_id, entry in chunk:
                provider_id = provider_id.strip()
                if not provider_id:
                    continue
                shard[provider_id] = entry
                self.index[provider_id] = shard_name
                self._index_dirty = True
            self._shards[shard_name] = shard
            self._dirty_shards.add(shard_name)
            shard_files += 1
        return shard_files

    def count_manually_verified(self) -> int:
        count = 0
        for shard_name in set(self.index.values()):
            shard = self._load_shard(shard_name)
            for entry in shard.values():
                if is_manually_verified_entry(entry):
                    count += 1
        return count

    def flush(self) -> None:
        for shard_name in sorted(self._dirty_shards):
            self._save_shard(shard_name)
        self._dirty_shards.clear()
        if self._index_dirty:
            self._save_index()
            self._index_dirty = False

    def __len__(self) -> int:
        return len(self.index)
