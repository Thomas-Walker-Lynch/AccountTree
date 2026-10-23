-- schema_storage.sql
--
-- The storage database, standard 5.0, at <tree dir>/database.sqlite3, where the
-- tree dir is <storage root>/home/AccountTree. It records the storage accounts
-- kept on the storage. It grants nothing on any system: which storage accounts
-- are the same accounts as system accounts on a system is recorded by that
-- system, in its system database.
--
--   database uuid   names this database; a copy of the database carries it too
--   database uid    a storage account's uid in this database's own numbering,
--                   which counts down from 1999999999; the account's files on the
--                   storage carry it
--
-- A database has no name for people: a name would travel with every copy of
-- the database and come to mislead. It is shown by its database uuid, series
-- number, head hash and the time of its last change.
-- Times are for people to read; the series number in identity is authoritative.

-- This database: its database uuid and the head of its hash chain. One row.
-- genesis_uuid is the database uuid it was made with, from which its chain
-- begins. A database made by branching from another gets a new database uuid,
-- and records the database it branched from and the head they parted after.
CREATE TABLE identity (
  database_uuid        TEXT NOT NULL,
  genesis_uuid         TEXT NOT NULL,
  branch_of            TEXT,              -- database uuid this one branched from
  branch_after_series  INTEGER,
  branch_after_head    TEXT,
  series               INTEGER NOT NULL,  -- series number of the last transaction
  head_hash            TEXT NOT NULL,     -- hash of the last transaction
  created              TEXT NOT NULL
);

-- The AccountTree standards this database follows.
CREATE TABLE standard (
  version  TEXT PRIMARY KEY
);

-- The storage accounts this database carries. A storage account's parent is
-- named by its name, and by its database uid when the parent is carried here.
CREATE TABLE account (
  database_uid         INTEGER PRIMARY KEY,
  name                 TEXT NOT NULL UNIQUE,
  parent_database_uid  INTEGER,              -- NULL when the parent is not carried here
  parent_name          TEXT,                 -- NULL for an account whose parent is root
  state                TEXT NOT NULL CHECK (state IN ('active' ,'deprecated' ,'deallocated')),
  created              TEXT NOT NULL,
  changed              TEXT NOT NULL
);

-- Every database uid this database has given out. None is given out twice.
CREATE TABLE database_uid (
  database_uid  INTEGER PRIMARY KEY,
  state         TEXT NOT NULL CHECK (state IN ('in use' ,'retired'))
);

-- The systems that have seen this database: those on which a storage account
-- of it has been made the same account as a system account, with when that
-- was first, and when the database was last attached or detached there. For
-- the owner of the storage, and for storage delete, which will not leave a
-- system mapping a database that no longer exists. Never authority: each
-- system's own system database is the record of which accounts are the same.
CREATE TABLE system_seen (
  system_uuid  TEXT PRIMARY KEY,
  first        TEXT NOT NULL,
  last         TEXT NOT NULL
);

-- Other databases this one knows, and their storage accounts that are the
-- same accounts as storage accounts here. Not used in 5.0.
CREATE TABLE acquaintance_database (
  database_uuid  TEXT PRIMARY KEY,
  introduced     TEXT NOT NULL
);

CREATE TABLE same_account_database (
  other_database_uuid  TEXT NOT NULL,
  other_database_uid   INTEGER NOT NULL,
  database_uid         INTEGER NOT NULL,
  PRIMARY KEY (other_database_uuid ,other_database_uid),
  UNIQUE (other_database_uuid ,database_uid)
);

-- Every change. hash covers previous_hash, series, description, change and nonce.
CREATE TABLE transaction_log (
  series         INTEGER PRIMARY KEY,
  previous_hash  TEXT NOT NULL,
  nonce          TEXT NOT NULL,
  description    TEXT NOT NULL,
  change         TEXT NOT NULL,     -- JSON list of [statement ,parameters]
  hash           TEXT NOT NULL,
  time           TEXT NOT NULL
);
