-- schema_system.sql
--
-- The system database, standard 5.0, at /var/lib/Account-Tree/system.sqlite3.
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

-- The Account-Tree standards this database follows.
CREATE TABLE standard (
  version  TEXT PRIMARY KEY
);

-- The range of system uids Account-Tree gives the system accounts it makes. One row.
CREATE TABLE system_uid__range (
  first  INTEGER NOT NULL,
  last   INTEGER NOT NULL
);

-- The system map: each system account that is the same account as a storage
-- account, the introduction of that storage account to this system. A row is
-- never deleted; when the two stop being the same account it is retired.
--
-- A row Account-Tree made the system account for is unplugged while its
-- removable storage is out: the system account is deleted, with the files it
-- left in the places listed in SameAccount.LIST__DIRPA__LOCAL, and its system
-- uid and gid are freed. What the system account had that the storage does
-- not carry, its password, shell, comment and supplementary groups, is kept
-- in system_account__saved, so that plugging the storage in again makes the
-- system account again as it was, with the next free system uid.
--
-- A system uid in a row in use or retired is never given to a system account
-- Account-Tree makes. A system account Account-Tree adopted, one that existed
-- before, stays when its storage is unplugged, and may be mapped again after
-- its row is retired.
CREATE TABLE system_map (
  id                    INTEGER PRIMARY KEY,
  system_uid            INTEGER,          -- NULL while unplugged
  system_gid            INTEGER,          -- NULL while unplugged
  system_account__name  TEXT NOT NULL,
  database_uuid         TEXT NOT NULL,
  database_uid          INTEGER NOT NULL,
  origin                TEXT NOT NULL CHECK (origin IN ('made' ,'adopted')),
  state                 TEXT NOT NULL CHECK (state IN ('in use' ,'unplugged' ,'retired')),
  mapped                TEXT NOT NULL,
  retired               TEXT,
  CHECK (state = 'retired' OR (state = 'unplugged') = (system_uid IS NULL))
);
CREATE UNIQUE INDEX system_map__system_uid__in_use ON system_map(system_uid) WHERE state = 'in use';
CREATE UNIQUE INDEX system_map__storage_account__introduced ON system_map(database_uuid ,database_uid) WHERE state IN ('in use' ,'unplugged');

-- Every head of each storage database this system has seen: when a storage
-- account of it was made the same account as a system account, and at each
-- attach and detach. At attach, comparing the storage's record with these
-- heads tells whether the storage has moved on since, gone back to an older
-- copy, or gone sideways, a branch.
CREATE TABLE storage_head__seen (
  id             INTEGER PRIMARY KEY,
  database_uuid  TEXT NOT NULL,
  series         INTEGER NOT NULL,
  head_hash      TEXT NOT NULL,
  event          TEXT NOT NULL CHECK (event IN ('mapped' ,'attached' ,'detached')),
  continuity     TEXT,             -- at attach: forward, backwards, sideways or unrelated
  seen           TEXT NOT NULL
);

-- How many child accounts each system account may have below it. A system
-- account with no row may have none. It is kept by name, because the system
-- uid of an account Account-Tree made changes each time its storage is plugged in.
CREATE TABLE account_limit (
  system_account__name  TEXT PRIMARY KEY,
  maximum               INTEGER NOT NULL
);

-- Each removable storage registered here: the LUKS2 partition that holds it,
-- found by its LUKS uuid, the storage database it carries, and where it is
-- mounted when it is unlocked. The storage name names the device-mapper
-- device, Account-Tree_<storage name>, and the log lines. A row is never
-- deleted; unregistering retires it. This table was added after the first
-- system databases were made, so its statements say IF NOT EXISTS, and
-- opening a system database runs them (see schema_added__text in Database.py).
CREATE TABLE IF NOT EXISTS storage_device (
  id             INTEGER PRIMARY KEY,
  storage_name   TEXT NOT NULL,
  luks_uuid      TEXT NOT NULL,
  database_uuid  TEXT NOT NULL,
  dirpa__mount      TEXT NOT NULL,
  state          TEXT NOT NULL CHECK (state IN ('registered' ,'retired')),
  registered     TEXT NOT NULL,
  retired        TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS storage_device__luks_registered ON storage_device(luks_uuid) WHERE state = 'registered';
CREATE UNIQUE INDEX IF NOT EXISTS storage_device__name_registered ON storage_device(storage_name) WHERE state = 'registered';

-- What a system account Account-Tree made had that its storage does not
-- carry, kept while the storage is unplugged, so that plugging it in again
-- makes the system account again as it was. A row is keyed on the storage
-- account, its database uid in its database, because the system uid was
-- freed at unplug. It is deleted when the system account is made again, or
-- when the introduction is retired. A field empty in /etc/shadow is NULL.
-- Added after the first system databases were made, like storage_device.
CREATE TABLE IF NOT EXISTS system_account__saved (
  database_uuid           TEXT NOT NULL,
  database_uid            INTEGER NOT NULL,
  comment                 TEXT NOT NULL,
  shell                   TEXT NOT NULL,
  password                TEXT NOT NULL,   -- the hash, as /etc/shadow holds it
  password__last_change   INTEGER,         -- days since 1970-01-01
  password__minimum_day   INTEGER,
  password__maximum_day   INTEGER,
  password__warning_day   INTEGER,
  password__inactive_day  INTEGER,
  account__expire         INTEGER,         -- days since 1970-01-01
  PRIMARY KEY (database_uuid ,database_uid)
);
-- The supplementary groups of each saved system account, a row for each group.
CREATE TABLE IF NOT EXISTS system_account__saved_group (
  database_uuid  TEXT NOT NULL,
  database_uid   INTEGER NOT NULL,
  group_name     TEXT NOT NULL,
  PRIMARY KEY (database_uuid ,database_uid ,group_name)
);

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
