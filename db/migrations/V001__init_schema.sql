-- =============================================================================
-- V001__init_schema.sql
-- Automated 3-Way Reconciliation Engine — core schema
-- Target: PostgreSQL 16+ (uses UNIQUE NULLS NOT DISTINCT, GENERATED columns)
--
-- Layout:
--   raw    : immutable, append-only copies of the 3 source feeds (OMS, BANK, 3PL)
--   recon  : reconciliation runs, matches, exceptions (mutable via workflow only)
--   audit  : append-only change log
--
-- Money convention: every amount is BIGINT in minor units of `currency`
-- (VND has no minor unit, so 150000 = 150,000 VND). Never FLOAT, never NUMERIC
-- rounding inside the engine.
-- =============================================================================

BEGIN;

CREATE SCHEMA raw;
CREATE SCHEMA recon;
CREATE SCHEMA audit;

-- -----------------------------------------------------------------------------
-- Enumerations
-- -----------------------------------------------------------------------------
CREATE TYPE recon.source_system     AS ENUM ('OMS', 'BANK', 'LOGISTICS');
CREATE TYPE recon.batch_status      AS ENUM ('RECEIVED', 'LOADING', 'LOADED', 'FAILED');
CREATE TYPE recon.payment_method    AS ENUM ('PREPAID', 'COD');
CREATE TYPE recon.run_status        AS ENUM ('RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED');
CREATE TYPE recon.match_method      AS ENUM ('EXACT', 'WINDOW_TOLERANCE', 'MANUAL_FORCE');
CREATE TYPE recon.match_status      AS ENUM ('ACTIVE', 'REVERSED');
CREATE TYPE recon.exception_type    AS ENUM (
    'AMOUNT_MISMATCH',    -- all legs found, amounts differ beyond tolerance
    'FEE_MISMATCH',       -- MDR / COD fee differs from contracted rate
    'MISSING_PAYMENT',    -- OMS order without a bank/PSP record
    'MISSING_SHIPMENT',   -- OMS order without a 3PL record
    'PHANTOM_PAYMENT',    -- bank record without an OMS order
    'PHANTOM_SHIPMENT',   -- 3PL record without an OMS order
    'DUPLICATE_RECORD',   -- same business key, different content
    'AMBIGUOUS_MATCH'     -- Pass 2 found more than one candidate
);
CREATE TYPE recon.exception_status  AS ENUM ('OPEN', 'IN_REVIEW', 'RESOLVED');
CREATE TYPE recon.resolution_action AS ENUM (
    'FORCE_MATCH', 'WRITE_OFF', 'TRIGGER_REFUND', 'DISMISS',
    'AUTO_RESOLVED'       -- a later run matched the record (e.g. late file)
);

-- -----------------------------------------------------------------------------
-- Shared trigger functions
-- -----------------------------------------------------------------------------

-- Rejects UPDATE / DELETE / TRUNCATE on append-only tables.
CREATE FUNCTION audit.forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% on %.% is not allowed (append-only table)',
        TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME
        USING ERRCODE = 'insufficient_privilege';
END $$;

-- =============================================================================
-- RAW LAYER
-- =============================================================================

-- One row per imported file. UNIQUE(source, file_sha256) makes re-importing
-- the same file a no-op (file-level idempotency). A FAILED batch is retried by
-- updating this row, not by inserting a new one.
CREATE TABLE raw.ingestion_batches (
    batch_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source          recon.source_system NOT NULL,
    file_name       text                NOT NULL,
    file_sha256     bytea               NOT NULL CHECK (octet_length(file_sha256) = 32),
    status          recon.batch_status  NOT NULL DEFAULT 'RECEIVED',
    rows_total      integer CHECK (rows_total >= 0),
    rows_inserted   integer CHECK (rows_inserted >= 0),
    rows_duplicate  integer CHECK (rows_duplicate >= 0),
    error_message   text,
    received_at     timestamptz NOT NULL DEFAULT now(),
    loaded_at       timestamptz,

    CONSTRAINT uq_batch_file   UNIQUE (source, file_sha256),
    CONSTRAINT ck_batch_loaded CHECK ((status = 'LOADED') = (loaded_at IS NOT NULL)),
    CONSTRAINT ck_batch_counts CHECK (rows_inserted + rows_duplicate <= rows_total),
    CONSTRAINT ck_batch_error  CHECK (status <> 'FAILED' OR error_message IS NOT NULL)
);

-- Every raw table carries:
--   row_hash    : SHA-256 of the normalized business fields (row-level idempotency;
--                 ingestion uses INSERT ... ON CONFLICT (row_hash) DO NOTHING)
--   raw_payload : the original line as received, for forensics
-- Business keys are deliberately NOT unique: two rows with the same key but
-- different content are a real-world problem (DUPLICATE_RECORD exception),
-- not something to reject at load time.

CREATE TABLE raw.oms_orders (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id        bigint NOT NULL REFERENCES raw.ingestion_batches (batch_id),
    row_hash        bytea  NOT NULL CHECK (octet_length(row_hash) = 32),
    order_id        text   NOT NULL,
    tracking_no     text,
    payment_method  recon.payment_method NOT NULL,
    currency        char(3) NOT NULL DEFAULT 'VND' CHECK (currency ~ '^[A-Z]{3}$'),
    order_amount    bigint NOT NULL CHECK (order_amount > 0),
    order_status    text   NOT NULL,
    ordered_at      timestamptz NOT NULL,
    raw_payload     jsonb  NOT NULL,
    ingested_at     timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT uq_oms_row_hash UNIQUE (row_hash)
);
CREATE INDEX ix_oms_order_id   ON raw.oms_orders (order_id);
CREATE INDEX ix_oms_tracking   ON raw.oms_orders (tracking_no) WHERE tracking_no IS NOT NULL;
CREATE INDEX ix_oms_ordered_at ON raw.oms_orders (ordered_at);
CREATE INDEX ix_oms_batch      ON raw.oms_orders (batch_id);

-- Payment gateway / bank statement lines. Amounts are stored as reported;
-- gross - fee <> net is a finding for the engine, not a load-time rejection.
CREATE TABLE raw.bank_transactions (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id        bigint NOT NULL REFERENCES raw.ingestion_batches (batch_id),
    row_hash        bytea  NOT NULL CHECK (octet_length(row_hash) = 32),
    txn_ref         text   NOT NULL,          -- bank / PSP transaction id
    order_ref       text,                     -- merchant ref parsed from memo; NULL = unattributable
    currency        char(3) NOT NULL DEFAULT 'VND' CHECK (currency ~ '^[A-Z]{3}$'),
    gross_amount    bigint NOT NULL CHECK (gross_amount > 0),
    mdr_fee         bigint NOT NULL DEFAULT 0 CHECK (mdr_fee >= 0),
    net_amount      bigint NOT NULL,
    txn_at          timestamptz NOT NULL,     -- authorization / transfer time
    value_date      date   NOT NULL,          -- settlement date (cut-off relevant)
    raw_payload     jsonb  NOT NULL,
    ingested_at     timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT uq_bank_row_hash UNIQUE (row_hash),
    CONSTRAINT ck_bank_fee_le_gross CHECK (mdr_fee <= gross_amount)
);
CREATE INDEX ix_bank_txn_ref        ON raw.bank_transactions (txn_ref);
CREATE INDEX ix_bank_order_ref_time ON raw.bank_transactions (order_ref, value_date) WHERE order_ref IS NOT NULL;
CREATE INDEX ix_bank_amount_time    ON raw.bank_transactions (gross_amount, value_date);  -- Pass 2 without a ref
CREATE INDEX ix_bank_batch          ON raw.bank_transactions (batch_id);

-- 3PL delivery report including COD collected and fees deducted.
CREATE TABLE raw.logistics_shipments (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id        bigint NOT NULL REFERENCES raw.ingestion_batches (batch_id),
    row_hash        bytea  NOT NULL CHECK (octet_length(row_hash) = 32),
    tracking_no     text   NOT NULL,
    order_ref       text,
    shipment_status text   NOT NULL,          -- DELIVERED / RETURNED / LOST ... (3PL vocabulary)
    currency        char(3) NOT NULL DEFAULT 'VND' CHECK (currency ~ '^[A-Z]{3}$'),
    cod_amount      bigint NOT NULL DEFAULT 0 CHECK (cod_amount >= 0),
    shipping_fee    bigint NOT NULL DEFAULT 0 CHECK (shipping_fee >= 0),
    cod_fee         bigint NOT NULL DEFAULT 0 CHECK (cod_fee >= 0),
    remittance_ref  text,                     -- links COD payout to a bank transfer
    picked_up_at    timestamptz,
    delivered_at    timestamptz,
    raw_payload     jsonb  NOT NULL,
    ingested_at     timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT uq_ship_row_hash UNIQUE (row_hash),
    CONSTRAINT ck_ship_timeline CHECK (delivered_at IS NULL OR picked_up_at IS NULL
                                       OR delivered_at >= picked_up_at)
);
CREATE INDEX ix_ship_tracking       ON raw.logistics_shipments (tracking_no);
CREATE INDEX ix_ship_order_ref_time ON raw.logistics_shipments (order_ref, delivered_at) WHERE order_ref IS NOT NULL;
CREATE INDEX ix_ship_cod_time       ON raw.logistics_shipments (cod_amount, delivered_at) WHERE cod_amount > 0;
CREATE INDEX ix_ship_remittance     ON raw.logistics_shipments (remittance_ref) WHERE remittance_ref IS NOT NULL;
CREATE INDEX ix_ship_batch          ON raw.logistics_shipments (batch_id);

-- Raw data is evidence: never edited, never deleted.
CREATE TRIGGER trg_oms_immutable   BEFORE UPDATE OR DELETE ON raw.oms_orders
    FOR EACH ROW EXECUTE FUNCTION audit.forbid_mutation();
CREATE TRIGGER trg_bank_immutable  BEFORE UPDATE OR DELETE ON raw.bank_transactions
    FOR EACH ROW EXECUTE FUNCTION audit.forbid_mutation();
CREATE TRIGGER trg_ship_immutable  BEFORE UPDATE OR DELETE ON raw.logistics_shipments
    FOR EACH ROW EXECUTE FUNCTION audit.forbid_mutation();
CREATE TRIGGER trg_oms_no_truncate  BEFORE TRUNCATE ON raw.oms_orders
    FOR EACH STATEMENT EXECUTE FUNCTION audit.forbid_mutation();
CREATE TRIGGER trg_bank_no_truncate BEFORE TRUNCATE ON raw.bank_transactions
    FOR EACH STATEMENT EXECUTE FUNCTION audit.forbid_mutation();
CREATE TRIGGER trg_ship_no_truncate BEFORE TRUNCATE ON raw.logistics_shipments
    FOR EACH STATEMENT EXECUTE FUNCTION audit.forbid_mutation();

-- =============================================================================
-- RECON LAYER
-- =============================================================================

-- A run reconciles one business-date period with a frozen parameter set.
-- idempotency_key = hash(period, params, input batch ids): calling "start run"
-- twice with the same inputs returns the existing run instead of a new one.
CREATE TABLE recon.runs (
    run_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    idempotency_key text   NOT NULL,
    period_start    date   NOT NULL,
    period_end      date   NOT NULL,
    period          daterange GENERATED ALWAYS AS (daterange(period_start, period_end, '[]')) STORED,
    engine_version  text   NOT NULL,
    params          jsonb  NOT NULL,          -- {"window_days":3,"amount_tolerance":500,...}
    status          recon.run_status NOT NULL DEFAULT 'RUNNING',
    stats           jsonb,                    -- per-pass counts, match rate, durations
    error_message   text,
    triggered_by    text   NOT NULL,
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,

    CONSTRAINT uq_run_idempotency UNIQUE (idempotency_key),
    CONSTRAINT ck_run_period      CHECK (period_end >= period_start),
    CONSTRAINT ck_run_finished    CHECK ((status = 'RUNNING') = (finished_at IS NULL)),
    CONSTRAINT ck_run_failed      CHECK (status <> 'FAILED' OR error_message IS NOT NULL),
    -- Two runs may never work on overlapping periods at the same time.
    CONSTRAINT ex_run_overlap     EXCLUDE USING gist (period WITH &&) WHERE (status = 'RUNNING')
);
CREATE INDEX ix_run_started ON recon.runs (started_at DESC);

-- A match is a group of source records that reconcile with each other.
-- Matches are never deleted; a manual override REVERSES one and creates another.
CREATE TABLE recon.matches (
    match_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id          bigint REFERENCES recon.runs (run_id),   -- NULL for manual matches
    pass_no         smallint,
    method          recon.match_method NOT NULL,
    status          recon.match_status NOT NULL DEFAULT 'ACTIVE',
    amount_delta    bigint   NOT NULL DEFAULT 0,  -- tolerated difference (Pass 2 / manual)
    day_gap         smallint NOT NULL DEFAULT 0 CHECK (day_gap >= 0),
    created_by      text     NOT NULL DEFAULT 'engine',
    created_at      timestamptz NOT NULL DEFAULT now(),
    reversed_at     timestamptz,
    reversed_by     text,
    reversal_reason text,

    CONSTRAINT ck_match_method_pass CHECK (
           (method = 'EXACT'            AND pass_no = 1    AND run_id IS NOT NULL)
        OR (method = 'WINDOW_TOLERANCE' AND pass_no = 2    AND run_id IS NOT NULL)
        OR (method = 'MANUAL_FORCE'     AND pass_no IS NULL)),
    CONSTRAINT ck_match_exact_is_exact CHECK (method <> 'EXACT' OR (amount_delta = 0 AND day_gap = 0)),
    CONSTRAINT ck_match_reversal CHECK (
        (status = 'REVERSED') = (reversed_at IS NOT NULL AND reversed_by IS NOT NULL
                                 AND reversal_reason IS NOT NULL))
);
CREATE INDEX ix_match_run ON recon.matches (run_id, pass_no);

-- One row per source record in a match (supports 1-1-1 now and N-M later,
-- e.g. split payments or one bank transfer settling many COD shipments).
-- Three typed FK columns instead of a polymorphic (source, id) pair so that
-- every link is protected by a real foreign key.
CREATE TABLE recon.match_items (
    match_item_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    match_id        bigint NOT NULL REFERENCES recon.matches (match_id),
    oms_order_id    bigint REFERENCES raw.oms_orders (id),
    bank_txn_id     bigint REFERENCES raw.bank_transactions (id),
    shipment_id     bigint REFERENCES raw.logistics_shipments (id),
    released_at     timestamptz,              -- set when the parent match is reversed

    CONSTRAINT ck_item_one_source CHECK (num_nonnulls(oms_order_id, bank_txn_id, shipment_id) = 1)
);
CREATE INDEX ix_item_match ON recon.match_items (match_id);
-- Core integrity guarantee: a source record belongs to at most ONE active match.
-- Re-running the engine can therefore never double-match a record.
CREATE UNIQUE INDEX uq_item_oms_active  ON recon.match_items (oms_order_id)
    WHERE oms_order_id IS NOT NULL AND released_at IS NULL;
CREATE UNIQUE INDEX uq_item_bank_active ON recon.match_items (bank_txn_id)
    WHERE bank_txn_id IS NOT NULL AND released_at IS NULL;
CREATE UNIQUE INDEX uq_item_ship_active ON recon.match_items (shipment_id)
    WHERE shipment_id IS NOT NULL AND released_at IS NULL;

-- A match must link at least two different sources. Checked at COMMIT
-- (deferred) because the match row is inserted before its items.
CREATE FUNCTION recon.check_match_legs() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    n_sources int;
BEGIN
    SELECT bool_or(oms_order_id IS NOT NULL)::int
         + bool_or(bank_txn_id  IS NOT NULL)::int
         + bool_or(shipment_id  IS NOT NULL)::int
      INTO n_sources
      FROM recon.match_items
     WHERE match_id = NEW.match_id;

    IF coalesce(n_sources, 0) < 2 THEN
        RAISE EXCEPTION 'match % must link at least 2 sources, found %',
            NEW.match_id, coalesce(n_sources, 0)
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER trg_match_min_legs
    AFTER INSERT ON recon.matches
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION recon.check_match_legs();

-- Reversal is one-way and releases the items so the records can be re-matched.
CREATE FUNCTION recon.on_match_status_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status = 'REVERSED' AND NEW.status <> 'REVERSED' THEN
        RAISE EXCEPTION 'match % is reversed and cannot be reactivated', OLD.match_id
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.status = 'ACTIVE' AND NEW.status = 'REVERSED' THEN
        UPDATE recon.match_items
           SET released_at = NEW.reversed_at
         WHERE match_id = NEW.match_id
           AND released_at IS NULL;
    END IF;
    RETURN NULL;
END $$;

CREATE TRIGGER trg_match_status
    AFTER UPDATE OF status ON recon.matches
    FOR EACH ROW EXECUTE FUNCTION recon.on_match_status_change();

-- Exceptions feed the accountant's workbench.
-- At most one unresolved exception per (type, records): a re-run upserts
-- last_seen_run_id instead of creating a duplicate ticket.
CREATE TABLE recon.exceptions (
    exception_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id            bigint NOT NULL REFERENCES recon.runs (run_id),   -- first detected
    last_seen_run_id  bigint NOT NULL REFERENCES recon.runs (run_id),
    exception_type    recon.exception_type   NOT NULL,
    status            recon.exception_status NOT NULL DEFAULT 'OPEN',
    oms_order_id      bigint REFERENCES raw.oms_orders (id),
    bank_txn_id       bigint REFERENCES raw.bank_transactions (id),
    shipment_id       bigint REFERENCES raw.logistics_shipments (id),
    expected_amount   bigint,
    actual_amount     bigint,
    delta_amount      bigint GENERATED ALWAYS AS (actual_amount - expected_amount) STORED,
    details           jsonb NOT NULL DEFAULT '{}',
    assigned_to       text,
    resolution        recon.resolution_action,
    resolution_note   text,
    resolved_match_id bigint REFERENCES recon.matches (match_id),
    resolved_by       text,
    resolved_at       timestamptz,
    version           integer NOT NULL DEFAULT 1,   -- optimistic locking for the workbench
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_exc_has_record CHECK (num_nonnulls(oms_order_id, bank_txn_id, shipment_id) >= 1),
    CONSTRAINT ck_exc_resolved   CHECK (
        (status = 'RESOLVED') = (resolution IS NOT NULL AND resolved_by IS NOT NULL
                                 AND resolved_at IS NOT NULL)),
    CONSTRAINT ck_exc_force_match CHECK (
        (resolution = 'FORCE_MATCH') = (resolved_match_id IS NOT NULL)),
    -- Money leaving the books needs a written justification.
    CONSTRAINT ck_exc_note_required CHECK (
        resolution IS NULL
        OR resolution NOT IN ('WRITE_OFF', 'DISMISS', 'TRIGGER_REFUND')
        OR coalesce(length(btrim(resolution_note)), 0) > 0)
);
CREATE UNIQUE INDEX uq_exc_open ON recon.exceptions
    (exception_type, oms_order_id, bank_txn_id, shipment_id) NULLS NOT DISTINCT
    WHERE status <> 'RESOLVED';
-- Workbench queue: "open items, by type, newest first".
CREATE INDEX ix_exc_queue  ON recon.exceptions (status, exception_type, created_at DESC);
CREATE INDEX ix_exc_assignee ON recon.exceptions (assigned_to, status) WHERE assigned_to IS NOT NULL;
CREATE INDEX ix_exc_run    ON recon.exceptions (run_id);
CREATE INDEX ix_exc_oms    ON recon.exceptions (oms_order_id) WHERE oms_order_id IS NOT NULL;
CREATE INDEX ix_exc_bank   ON recon.exceptions (bank_txn_id)  WHERE bank_txn_id  IS NOT NULL;
CREATE INDEX ix_exc_ship   ON recon.exceptions (shipment_id)  WHERE shipment_id  IS NOT NULL;

CREATE FUNCTION recon.touch_exception() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status = 'RESOLVED' AND NEW.status <> 'RESOLVED' THEN
        RAISE EXCEPTION 'exception % is resolved and cannot be reopened', OLD.exception_id
            USING ERRCODE = 'check_violation';
    END IF;
    NEW.version    := OLD.version + 1;
    NEW.updated_at := now();
    RETURN NEW;
END $$;

CREATE TRIGGER trg_exc_touch
    BEFORE UPDATE ON recon.exceptions
    FOR EACH ROW EXECUTE FUNCTION recon.touch_exception();

-- =============================================================================
-- AUDIT LAYER
-- =============================================================================

-- The API sets these per transaction:
--   SET LOCAL app.actor = 'accountant@shop.vn';
--   SET LOCAL app.request_id = '<uuid>';
CREATE TABLE audit.audit_log (
    audit_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    txid         bigint      NOT NULL DEFAULT txid_current(),
    actor        text        NOT NULL,
    request_id   text,
    action       text        NOT NULL CHECK (action IN ('INSERT', 'UPDATE', 'DELETE')),
    table_name   text        NOT NULL,
    row_pk       bigint      NOT NULL,
    old_row      jsonb,
    new_row      jsonb,
    changed_cols text[]
);
CREATE INDEX ix_audit_entity ON audit.audit_log (table_name, row_pk, occurred_at);
CREATE INDEX ix_audit_actor  ON audit.audit_log (actor, occurred_at);

CREATE TRIGGER trg_audit_immutable   BEFORE UPDATE OR DELETE ON audit.audit_log
    FOR EACH ROW EXECUTE FUNCTION audit.forbid_mutation();
CREATE TRIGGER trg_audit_no_truncate BEFORE TRUNCATE ON audit.audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit.forbid_mutation();

-- Generic row audit. TG_ARGV[0] = name of the table's bigint primary key column.
CREATE FUNCTION audit.log_change() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    old_j jsonb := CASE WHEN TG_OP <> 'INSERT' THEN to_jsonb(OLD) END;
    new_j jsonb := CASE WHEN TG_OP <> 'DELETE' THEN to_jsonb(NEW) END;
BEGIN
    INSERT INTO audit.audit_log
        (actor, request_id, action, table_name, row_pk, old_row, new_row, changed_cols)
    VALUES (
        coalesce(nullif(current_setting('app.actor', true), ''), session_user),
        nullif(current_setting('app.request_id', true), ''),
        TG_OP,
        TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME,
        (coalesce(new_j, old_j) ->> TG_ARGV[0])::bigint,
        old_j,
        new_j,
        CASE WHEN TG_OP = 'UPDATE' THEN
            ARRAY(SELECT n.key FROM jsonb_each(new_j) n
                  WHERE n.value IS DISTINCT FROM old_j -> n.key)
        END
    );
    RETURN NULL;
END $$;

-- Engine INSERTs (100k+ rows per run) are not row-audited: their provenance is
-- already recorded by run_id / created_by. Every human-driven change
-- (UPDATE/DELETE) is. Runs are few and fully audited.
CREATE TRIGGER trg_audit_runs        AFTER INSERT OR UPDATE OR DELETE ON recon.runs
    FOR EACH ROW EXECUTE FUNCTION audit.log_change('run_id');
CREATE TRIGGER trg_audit_matches     AFTER UPDATE OR DELETE ON recon.matches
    FOR EACH ROW EXECUTE FUNCTION audit.log_change('match_id');
CREATE TRIGGER trg_audit_match_items AFTER UPDATE OR DELETE ON recon.match_items
    FOR EACH ROW EXECUTE FUNCTION audit.log_change('match_item_id');
CREATE TRIGGER trg_audit_exceptions  AFTER UPDATE OR DELETE ON recon.exceptions
    FOR EACH ROW EXECUTE FUNCTION audit.log_change('exception_id');
CREATE TRIGGER trg_audit_batches     AFTER INSERT OR UPDATE OR DELETE ON raw.ingestion_batches
    FOR EACH ROW EXECUTE FUNCTION audit.log_change('batch_id');

-- Matches, items and exceptions are corrected by new rows / state changes, never deleted.
CREATE TRIGGER trg_matches_no_delete    BEFORE DELETE ON recon.matches
    FOR EACH ROW EXECUTE FUNCTION audit.forbid_mutation();
CREATE TRIGGER trg_items_no_delete      BEFORE DELETE ON recon.match_items
    FOR EACH ROW EXECUTE FUNCTION audit.forbid_mutation();
CREATE TRIGGER trg_exceptions_no_delete BEFORE DELETE ON recon.exceptions
    FOR EACH ROW EXECUTE FUNCTION audit.forbid_mutation();

COMMIT;
