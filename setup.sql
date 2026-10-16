-- OPTIONAL. Run state is checkpointed locally by LangGraph (.state/runs.sqlite).
-- These tables add Snowflake-side artifacts, observability and an audit copy of run state.
-- Replace <APP_DB>.<APP_SCHEMA> and run with a role that can create them.

CREATE DATABASE IF NOT EXISTS <APP_DB>;
CREATE SCHEMA IF NOT EXISTS <APP_DB>.<APP_SCHEMA>;

CREATE TABLE IF NOT EXISTS <APP_DB>.<APP_SCHEMA>.RUN_CHECKPOINTS (
    RUN_ID     VARCHAR NOT NULL PRIMARY KEY,
    SLICE_ID   VARCHAR,
    STATUS     VARCHAR,        -- running | awaiting_human | done | failed | aborted
    STAGE      VARCHAR,
    STATE      VARIANT,        -- full RunState JSON
    CREATED_AT TIMESTAMP_LTZ,
    UPDATED_AT TIMESTAMP_LTZ
);

CREATE TABLE IF NOT EXISTS <APP_DB>.<APP_SCHEMA>.ARTIFACTS (
    RUN_ID     VARCHAR NOT NULL,
    STAGE      VARCHAR NOT NULL,
    ITERATION  NUMBER  NOT NULL,
    NAME       VARCHAR NOT NULL,
    CONTENT    VARCHAR,
    CREATED_AT TIMESTAMP_LTZ,
    PRIMARY KEY (RUN_ID, STAGE, ITERATION, NAME)
);

CREATE TABLE IF NOT EXISTS <APP_DB>.<APP_SCHEMA>.OBS_LLM_CALLS (
    RUN_ID     VARCHAR,
    STAGE      VARCHAR,
    ROLE       VARCHAR,
    MODEL      VARCHAR,
    TOKENS_IN  NUMBER,
    TOKENS_OUT NUMBER,
    LATENCY_MS NUMBER,
    RETRIES    NUMBER,
    STATUS     VARCHAR,
    ERROR      VARCHAR,
    QUERY_ID   VARCHAR,        -- Snowflake query id: joins to ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY for billed credits
    TS         TIMESTAMP_LTZ
);

CREATE TABLE IF NOT EXISTS <APP_DB>.<APP_SCHEMA>.OBS_NODES (
    RUN_ID      VARCHAR,
    STAGE       VARCHAR,
    NODE        VARCHAR,
    STATUS      VARCHAR,
    DURATION_MS NUMBER,
    DETAIL      VARCHAR,
    TS          TIMESTAMP_LTZ
);

-- The connecting role needs, on each schema you migrate:
--   USAGE on database/schema, REFERENCES or SELECT on tables/views, MONITOR on tasks/dynamic tables,
--   USAGE on procedures (for GET_DDL), and SELECT if you enable live parity profiling.
-- Cortex: GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO ROLE <APP_OWNER_ROLE>;

-- Existing installations created before query ids were recorded:
-- ALTER TABLE <APP_DB>.<APP_SCHEMA>.OBS_LLM_CALLS ADD COLUMN IF NOT EXISTS QUERY_ID VARCHAR;
-- Financial page billed credits read SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY:
-- GRANT DATABASE ROLE SNOWFLAKE.USAGE_VIEWER TO ROLE <APP_OWNER_ROLE>;
