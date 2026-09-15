version = 73
description = "Clean up hardcoded stale macro fallback defaults from kv_cache"
sql = """
DELETE FROM kv_cache WHERE key = 'macro_spx' AND value = '5150.0';
DELETE FROM kv_cache WHERE key = 'macro_gamma_flip_line' AND value = '5180.0';
DELETE FROM kv_cache WHERE key = 'macro_spy_spot' AND value = '510.0';
DELETE FROM kv_cache WHERE key = 'macro_spy_gamma_flip' AND value = '515.0';
"""
