-- schema_system.sql
--
-- The system database, standard 5.0, at /var/lib/AccountTree/system.sqlite3.
-- It is the system's own record, and the authority for which storage accounts
-- are the same accounts as system accounts on this system. It is kept under
-- /var, which holds state, not under /etc, which holds configuration: a system
-- whose root file system is rolled back from a snapshot keeps /var as it was,
-- so the record of the system uids already given out is not rolled back with
-- /etc/passwd.
--
--   system uuid     names this system
--   system uid      a system account's uid, in /etc/passwd
--   database uuid   names a storage database
--   database uid    a storage account's uid in that database's own numbering
-- Times are for people to read; the series number in identity is authoritative.

-- This system: its system uuid and the head of its hash chain. One row.
-- genesis_uuid is the system uuid, from which the chain begins.
CREATE TABLE identity (
  system_uuid   TEXT NOT NULL,
  genesis_uuid  TEXT NOT NULL,
  series        INTEGER NOT NULL,
  head_hash     TEXT NOT NULL,
  created       TEXT NOT NULL
);

-- The AccountTree standards this database follows.
CREATE TABLE standard (
  version  TEXT PRIMARY KEY
);

-- The range of system uids AccountTree gives the system accounts it makes. One row.
CREATE TABLE system_uid_range (
  first  INTEGER NOT NULL,
  last   INTEGER NOT NULL
);

-- The system map: each system account that is the same account as a storage
-- account. A row is never deleted; when the two stop being the same account it
-- is retired. A system uid in any row, in use or retired, is never given to a
-- system account AccountTree makes. A system account AccountTree adopted, one
-- that existed before, may be mapped again after its row is retired.
CREATE TABLE system_map (
  id                   INTEGER PRIMARY KEY,
  system_uid           INTEGER NOT NULL,
  system_gid           INTEGER NOT NULL,
  system_account_name  TEXT NOT NULL,   -- kept so that a system account lost from /etc/passwd can be made again
  database_uuid        TEXT NOT NULL,
  database_uid         INTEGER NOT NULL,
  origin               TEXT NOT NULL CHECK (origin IN ('made' ,'adopted')),
  state                TEXT NOT NULL CHECK (state IN ('in use' ,'retired')),
  mapped               TEXT NOT NULL,
  retired              TEXT
);
CREATE UNIQUE INDEX system_map_system_uid_in_use ON system_map(system_uid) WHERE state = 'in use';
CREATE UNIQUE INDEX system_map_storage_account_in_use ON system_map(database_uuid ,database_uid) WHERE state = 'in use';

-- Every head of each storage database this system has seen: when a storage
-- account of it was made the same account as a system account, and at each
-- attach and detach. At attach, comparing the storage's record with these
-- heads tells whether the storage has moved on since, gone back to an older
-- copy, or gone sideways, a branch.
CREATE TABLE storage_head_seen (
  id             INTEGER PRIMARY KEY,
  database_uuid  TEXT NOT NULL,
  series         INTEGER NOT NULL,
  head_hash      TEXT NOT NULL,
  event          TEXT NOT NULL CHECK (event IN ('mapped' ,'attached' ,'detached')),
  continuity     TEXT,             -- at attach: forward, backwards, sideways or unrelated
  seen           TEXT NOT NULL
);

-- How many child accounts each system account may have below it. A system
-- account with no row may have none.
CREATE TABLE account_limit (
  system_uid  INTEGER PRIMARY KEY,
  maximum     INTEGER NOT NULL
);

-- Each removable storage registered here: the LUKS2 partition that holds it,
-- found by its LUKS uuid, the storage database it carries, and where it is
-- mounted when it is unlocked. The storage name names the device-mapper
-- device, AccountTree_<storage name>, and the log lines. A row is never
-- deleted; unregistering retires it. This table was added after the first
-- system databases were made, so its statements say IF NOT EXISTS, and
-- opening a system database runs them (see schema_added_text in database.py).
CREATE TABLE IF NOT EXISTS storage_device (
  id             INTEGER PRIMARY KEY,
  storage_name   TEXT NOT NULL,
  luks_uuid      TEXT NOT NULL,
  database_uuid  TEXT NOT NULL,
  mount_dir      TEXT NOT NULL,
  state          TEXT NOT NULL CHECK (state IN ('registered' ,'retired')),
  registered     TEXT NOT NULL,
  retired        TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS storage_device_luks_registered ON storage_device(luks_uuid) WHERE state = 'registered';
CREATE UNIQUE INDEX IF NOT EXISTS storage_device_name_registered ON storage_device(storage_name) WHERE state = 'registered';

-- Every change. hash covers previous_hash, series, description, change and nonce.
CREATE TABLE transaction_log (
  series         INTEGER PRIMARY KEY,
  previous_hash  TEXT NOT NULL,
  nonce          TEXT NOT NULL,
  description    TEXT NOT NULL,
  change         TEXT NOT NULL,
  hash           TEXT NOT NULL,
  time           TEXT NOT NULL
);
