CREATE TABLE IF NOT EXISTS pg_rate_limits (
    identifier TEXT NOT NULL,
    group_name TEXT NOT NULL,
    window_seconds BIGINT NOT NULL CHECK (window_seconds > 0),
    window_start BIGINT NOT NULL,
    window_end BIGINT NOT NULL,
    count BIGINT NOT NULL CHECK (count >= 0),
    cleanup_after BIGINT NOT NULL,
    policy_version TEXT NOT NULL,
    PRIMARY KEY (identifier, group_name, window_seconds, window_start)
);
CREATE INDEX IF NOT EXISTS pg_rate_limits_cleanup ON pg_rate_limits(cleanup_after);
