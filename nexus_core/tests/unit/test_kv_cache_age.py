"""Unit tests for database.cache.get_kv_cache_with_age — the additive read
helper backing the options-data freshness/timestamp display work (surfaces
kv_cache.updated_at as an age-in-seconds alongside the existing value, without
changing get_kv_cache()'s own behavior or the stored value shape)."""

from database.cache import get_kv_cache_with_age, save_kv_cache


async def test_get_kv_cache_with_age_missing_key_returns_none_tuple() -> None:
    value, age_seconds = get_kv_cache_with_age("does_not_exist_key")
    assert value is None
    assert age_seconds is None


async def test_get_kv_cache_with_age_returns_value_and_small_age() -> None:
    assert await save_kv_cache("uoa_TESTSYM", [{"strike": 100.0}]) is True

    value, age_seconds = get_kv_cache_with_age("uoa_TESTSYM")
    assert value == [{"strike": 100.0}]
    assert age_seconds is not None
    # 剛寫入，年齡應接近 0 秒，給予寬鬆容差以避免測試環境時鐘/延遲造成偶發失敗。
    assert 0.0 <= age_seconds < 30.0


async def test_get_kv_cache_with_age_preserves_arbitrary_json_value_shape() -> None:
    await save_kv_cache("dp_poc_TESTSYM", 123.45)

    value, age_seconds = get_kv_cache_with_age("dp_poc_TESTSYM")
    assert value == 123.45
    assert age_seconds is not None
