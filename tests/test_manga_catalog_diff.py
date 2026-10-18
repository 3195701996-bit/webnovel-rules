from engine.manga.catalog_diff import compare_catalogs


def test_missing_downloads_are_not_all_new_catalog_items():
    old = [{"id": "one", "name": "第1话"}, {"id": "two", "name": "第2话"}]
    new = old + [{"id": "three", "name": "第3话"}]
    result = compare_catalogs(new, old, new[1:])
    assert result["baseline_available"]
    assert result["added_missing_count"] == 1
    assert result["existing_missing_count"] == 1


def test_same_name_different_id_is_warning_not_automatic_merge():
    old = [{"id": "old", "name": "第41话"}]
    new = old + [{"id": "other", "name": "第41话"}]
    result = compare_catalogs(new, old, [new[1]])
    assert result["same_name_new_id_count"] == 1
    assert result["duplicate_name_count"] == 1
    assert result["added_missing_count"] == 1
    assert len(new) == 2


def test_rename_keeps_identity_and_is_reported():
    result = compare_catalogs([{"id": "same", "name": "第42话"}],
                              [{"id": "same", "name": "第41话"}], [])
    assert result["renamed_count"] == 1
    assert result["renamed"][0]["previous_name"] == "第41话"
    assert result["added_missing_count"] == 0


def test_no_baseline_does_not_invent_new_episode_count():
    rows = [{"id": "one", "name": "第1话"}]
    result = compare_catalogs(rows, [], rows)
    assert not result["baseline_available"]
    assert result["added_missing_count"] == 0


def test_warning_details_are_bounded_but_counts_are_complete():
    old = [{"id": str(i), "name": "旧" + str(i)} for i in range(30)]
    new = [{"id": str(i), "name": "新" + str(i)} for i in range(30)]
    result = compare_catalogs(new, old, [])
    assert result["renamed_count"] == 30
    assert len(result["renamed"]) == 20
