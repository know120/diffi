from __future__ import annotations

from typing import Any


def collect_fields(obj: Any, prefix: str = "") -> list[dict]:
    fields: list[dict] = []
    if not isinstance(obj, dict):
        return fields
    for key, value in obj.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            fields.append({"path": path, "type": "object"})
            fields.extend(collect_fields(value, path))
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            fields.append({"path": path, "type": "array<object>"})
            fields.extend(collect_fields(value[0], f"{path}[]"))
        else:
            t = "array" if isinstance(value, list) else type(value).__name__
            fields.append({"path": path, "type": t})
    return fields


def _is_ignored(path: str, ignore_fields: set[str]) -> bool:
    for ignored in ignore_fields:
        if path == ignored or path.startswith(ignored + ".") or path.startswith(ignored + "["):
            return True
    return False


def deep_compare(
    old_obj: Any, new_obj: Any, path: str = "",
    ignore_fields: set[str] | None = None,
) -> dict:
    missing: list[dict] = []
    extra: list[dict] = []
    type_changes: list[dict] = []

    if ignore_fields is None:
        ignore_fields = set()

    all_keys: set[str] = set()
    if isinstance(old_obj, dict):
        all_keys.update(old_obj.keys())
    if isinstance(new_obj, dict):
        all_keys.update(new_obj.keys())

    for key in sorted(all_keys):
        current_path = f"{path}.{key}" if path else key

        if _is_ignored(current_path, ignore_fields):
            continue

        old_exists = isinstance(old_obj, dict) and key in old_obj
        new_exists = isinstance(new_obj, dict) and key in new_obj

        if not old_exists and new_exists:
            extra.append({"path": current_path, "value": new_obj[key]})
            continue
        if old_exists and not new_exists:
            missing.append({"path": current_path, "value": old_obj[key]})
            continue
        if not old_exists and not new_exists:
            continue

        old_val = old_obj[key]
        new_val = new_obj[key]

        if old_val is None and new_val is None:
            continue
        if old_val is None or new_val is None:
            type_changes.append({
                "path": current_path,
                "oldType": "null" if old_val is None else type(old_val).__name__,
                "newType": "null" if new_val is None else type(new_val).__name__,
                "oldValue": old_val,
                "newValue": new_val,
            })
            continue

        if isinstance(old_val, list) and isinstance(new_val, list):
            if not old_val and not new_val:
                continue
            if old_val and new_val and isinstance(old_val[0], dict) and isinstance(new_val[0], dict):
                max_len = max(len(old_val), len(new_val))
                for i in range(max_len):
                    item_path = f"{current_path}[{i}]"
                    if _is_ignored(item_path, ignore_fields):
                        continue
                    if i >= len(old_val):
                        extra.append({"path": item_path, "value": new_val[i]})
                    elif i >= len(new_val):
                        missing.append({"path": item_path, "value": old_val[i]})
                    else:
                        sub = deep_compare(old_val[i], new_val[i], item_path, ignore_fields)
                        missing.extend(sub["missing"])
                        extra.extend(sub["extra"])
                        type_changes.extend(sub["typeChanges"])
            else:
                old_set = {str(v) for v in old_val}
                new_set = {str(v) for v in new_val}
                for item in old_val:
                    if str(item) not in new_set:
                        missing.append({"path": current_path, "value": item})
                for item in new_val:
                    if str(item) not in old_set:
                        extra.append({"path": current_path, "value": item})
            continue

        if isinstance(old_val, dict) and isinstance(new_val, dict):
            sub = deep_compare(old_val, new_val, current_path, ignore_fields)
            missing.extend(sub["missing"])
            extra.extend(sub["extra"])
            type_changes.extend(sub["typeChanges"])
            continue

        if type(old_val) is not type(new_val):
            type_changes.append({
                "path": current_path,
                "oldType": type(old_val).__name__,
                "newType": type(new_val).__name__,
                "oldValue": old_val,
                "newValue": new_val,
            })

    return {"missing": missing, "extra": extra, "typeChanges": type_changes}


def apply_field_mappings(data: Any, mappings: list[dict]) -> Any:
    if not mappings or not isinstance(data, dict):
        return data
    if isinstance(data, list):
        return [apply_field_mappings(item, mappings) for item in data]

    mapping_dict: dict[str, str] = {}
    for m in mappings:
        if m.get("oldPath") and m.get("newPath"):
            mapping_dict[m["newPath"]] = m["oldPath"]

    def _remap(obj: Any, path: str = "") -> Any:
        if not isinstance(obj, dict):
            return obj
        if isinstance(obj, list):
            return [_remap(item, f"{path}[{i}]") for i, item in enumerate(obj)]
        result: dict[str, Any] = {}
        for key, val in obj.items():
            cur_path = f"{path}.{key}" if path else key
            mapped_key = mapping_dict.get(cur_path, key)
            result[mapped_key] = _remap(val, cur_path)
        return result

    return _remap(data)
