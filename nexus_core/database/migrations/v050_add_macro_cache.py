version = 50
description = "Initialize default macro indicators in kv_cache table"
sql = """
INSERT OR IGNORE INTO kv_cache (key, value, updated_at) VALUES
('macro_spx', '5150.0', CURRENT_TIMESTAMP),
('macro_vix', '18.0', CURRENT_TIMESTAMP),
('macro_us10y', '4.25', CURRENT_TIMESTAMP),
('macro_wti', '75.0', CURRENT_TIMESTAMP),
('macro_rrp', '420.5', CURRENT_TIMESTAMP),
('macro_fed_balance', '7.25', CURRENT_TIMESTAMP),
('macro_cpi_nfp_calendar', '"2026-06-18 (CPI), 2026-07-03 (NFP)"', CURRENT_TIMESTAMP),
('macro_fear_greed', '48.0', CURRENT_TIMESTAMP),
('macro_gamma_flip_line', '5180.0', CURRENT_TIMESTAMP),
('macro_spy_spot', '510.0', CURRENT_TIMESTAMP),
('macro_spy_gamma_flip', '515.0', CURRENT_TIMESTAMP),
('macro_vts_ratio', '0.95', CURRENT_TIMESTAMP),
('macro_uer', '4.0', CURRENT_TIMESTAMP),
('macro_sahm_rule', '0.35', CURRENT_TIMESTAMP),
('macro_cpi_deviation', '0.0', CURRENT_TIMESTAMP),
('macro_rrp_change_30d', '0.05', CURRENT_TIMESTAMP);
"""
