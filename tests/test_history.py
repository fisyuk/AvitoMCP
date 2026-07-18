from pathlib import Path

from avito_mcp.history import QueryHistory


def test_history_marks_only_unseen_ids(tmp_path: Path) -> None:
    history = QueryHistory(tmp_path / "history.sqlite3")
    try:
        first = history.record_run(
            fingerprint="same-search",
            query="книга",
            max_price_rub=2000,
            search_scope="https://www.avito.ru/all/knigi",
            item_ids=["1", "2", "2"],
        )
        second = history.record_run(
            fingerprint="same-search",
            query="книга",
            max_price_rub=2000,
            search_scope="https://www.avito.ru/all/knigi",
            item_ids=["2", "3"],
        )
        assert first.initial_run is True
        assert first.new_item_ids == frozenset({"1", "2"})
        assert second.initial_run is False
        assert second.new_item_ids == frozenset({"3"})
        assert history.summary() == {"searches": 1, "runs": 2, "seen_items": 3}
    finally:
        history.close()

