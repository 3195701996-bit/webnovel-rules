"""Read-only catalog comparison. IDs are authoritative; names only flag ambiguity."""


def compare_catalogs(current, previous, missing, name_key=lambda value: str(value or "").strip()):
    old = {str(row.get("id")): row for row in previous
           if isinstance(row, dict) and row.get("id")}
    present = {str(row.get("id")): row for row in current
               if isinstance(row, dict) and row.get("id")}
    missing_ids = {str(row.get("id")) for row in missing
                   if isinstance(row, dict) and row.get("id")}
    old_names = {name_key(row.get("name")) for row in old.values()} - {""}
    new_ids = set(present) - set(old) if old else set()
    renamed = []
    for key in sorted(set(old) & set(present)):
        before, after = old[key].get("name", ""), present[key].get("name", "")
        if name_key(before) != name_key(after):
            renamed.append({"id": key, "name": after, "previous_name": before})
    same_name = [{"id": key, "name": present[key].get("name", "")}
                 for key in sorted(new_ids) if name_key(present[key].get("name")) in old_names]
    names = {}
    for key, row in present.items():
        name = name_key(row.get("name"))
        if name:
            names.setdefault(name, []).append(key)
    duplicates = [{"name": name, "ids": ids} for name, ids in names.items() if len(ids) > 1]
    return {
        "baseline_available": bool(old),
        "added_missing_count": len(new_ids & missing_ids),
        "existing_missing_count": len(set(old) & missing_ids),
        "renamed_count": len(renamed),
        "same_name_new_id_count": len(same_name),
        "duplicate_name_count": len(duplicates),
        "renamed": renamed[:20],
        "same_name_new_ids": same_name[:20],
        "duplicate_names": duplicates[:20],
    }
