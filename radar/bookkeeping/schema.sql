-- AI Bookkeeping schema
-- transaction.type: expense | income | transfer_in | transfer_out | balance_adjust | debt_change
-- transaction.amount: signed real (as in the bookkeeping app; negative = refund / adjustment)

CREATE TABLE IF NOT EXISTS accounts (
  id        INTEGER PRIMARY KEY,
  name      TEXT NOT NULL UNIQUE,
  type      TEXT NOT NULL DEFAULT 'unknown',
  currency  TEXT NOT NULL,
  owner     TEXT,
  archived  INTEGER NOT NULL DEFAULT 0,
  expected_balance     REAL,        -- the"actual balance" (copied from the bank app)
  expected_balance_at  TEXT,        -- last updated
  statement_day        INTEGER,     -- statement day (day of month)
  payment_due_day      INTEGER      -- payment due day (day of month)
);

CREATE TABLE IF NOT EXISTS categories (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  parent_id  INTEGER REFERENCES categories(id),
  UNIQUE(name, parent_id)
);

CREATE TABLE IF NOT EXISTS members (
  id    INTEGER PRIMARY KEY,
  name  TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS imports (
  id                INTEGER PRIMARY KEY,
  source_type       TEXT NOT NULL,
  filename          TEXT NOT NULL,
  imported_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  rows_total        INTEGER,
  rows_inserted     INTEGER,
  rows_soft_deleted INTEGER,
  rows_skipped      INTEGER,
  status            TEXT,
  meta              TEXT,
  account_id        INTEGER REFERENCES accounts(id)   -- only pdf_* types; NULL for multi-account csv
);

CREATE TABLE IF NOT EXISTS transactions (
  id            INTEGER PRIMARY KEY,
  type          TEXT NOT NULL,
  occurred_at   TEXT NOT NULL,
  amount        REAL NOT NULL,
  currency      TEXT NOT NULL,
  account_id    INTEGER REFERENCES accounts(id),
  category_id   INTEGER REFERENCES categories(id),
  member_id     INTEGER REFERENCES members(id),
  note          TEXT,
  project       TEXT,
  related_uuid  TEXT,
  source        TEXT NOT NULL,
  import_id     INTEGER REFERENCES imports(id),
  hash          TEXT UNIQUE,
  edited_at     TEXT,
  deleted_at    TEXT,
  created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_tx_date     ON transactions(occurred_at);
CREATE INDEX IF NOT EXISTS idx_tx_type     ON transactions(type, occurred_at);
CREATE INDEX IF NOT EXISTS idx_tx_account  ON transactions(account_id);
CREATE INDEX IF NOT EXISTS idx_tx_category ON transactions(category_id);
CREATE INDEX IF NOT EXISTS idx_tx_uuid     ON transactions(related_uuid);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
INSERT OR IGNORE INTO settings (key, value) VALUES ('base_currency', 'USD');

-- Budgets (per top-level category × year, in USD)
CREATE TABLE IF NOT EXISTS budgets (
  id            INTEGER PRIMARY KEY,
  category_name TEXT NOT NULL,                    -- top-level category name
  year          INTEGER NOT NULL,
  amount_usd    REAL NOT NULL,                    -- yearly budget USD
  note          TEXT,
  updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(category_name, year)
);

-- Bank PDF import staging table (to reconcile)
CREATE TABLE IF NOT EXISTS pending_transactions (
  id              INTEGER PRIMARY KEY,
  occurred_at     TEXT NOT NULL,
  amount          REAL NOT NULL,           -- signed: + in / - out
  currency        TEXT NOT NULL,
  account_id      INTEGER REFERENCES accounts(id),
  description     TEXT,
  source          TEXT NOT NULL,           -- import source, e.g. csv
  import_id       INTEGER REFERENCES imports(id),
  raw             TEXT,                    -- raw PDF line (for debugging)
  status          TEXT NOT NULL DEFAULT 'pending',  -- pending | matched | confirmed | discarded
  matched_tx_id   INTEGER REFERENCES transactions(id),
  resolved_at     TEXT,
  hash            TEXT UNIQUE,             -- stops the same PDF being uploaded twice
  created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_transactions(status);
CREATE INDEX IF NOT EXISTS idx_pending_account ON pending_transactions(account_id);

CREATE TABLE IF NOT EXISTS agent_messages (
  id          INTEGER PRIMARY KEY,
  role        TEXT NOT NULL,
  content     TEXT NOT NULL,
  created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Ledger panel: monthly to-dos (e.g. everything due on the 10th), checked off per month
CREATE TABLE IF NOT EXISTS monthly_tasks (
  id      INTEGER PRIMARY KEY,
  title   TEXT NOT NULL,
  note    TEXT,
  day     INTEGER NOT NULL DEFAULT 10,   -- day of month it becomes due
  sort    INTEGER NOT NULL DEFAULT 0,
  created TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS monthly_done (
  task_id INTEGER NOT NULL REFERENCES monthly_tasks(id) ON DELETE CASCADE,
  ym      TEXT NOT NULL,                 -- 2026-11
  done_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (task_id, ym)
);

-- FI: monthly asset snapshots (entered by hand), one row per item per month
CREATE TABLE IF NOT EXISTS fi_snapshots (
  ym       TEXT NOT NULL,          -- 2026-11
  item     TEXT NOT NULL,          -- e.g. my brokerage, partner 401k, cash
  owner    TEXT NOT NULL DEFAULT 'Household',   -- Household | Me
  currency TEXT NOT NULL DEFAULT 'USD',
  amount   REAL NOT NULL,
  updated  TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (ym, item)
);

-- FI: the user's own goals (separate from the household goal)
CREATE TABLE IF NOT EXISTS fi_goals (
  id       INTEGER PRIMARY KEY,
  name     TEXT NOT NULL,
  target   REAL,                 -- USD; NULL = not set yet
  monthly  REAL,                 -- USD/month; NULL = from ledger (net transfers into the user's investment accounts)
  deadline TEXT,                 -- YYYY-MM, optional
  items    TEXT,                 -- comma-separated snapshot item names; NULL/'' = all items owned by Me
  sort     INTEGER NOT NULL DEFAULT 0
);
