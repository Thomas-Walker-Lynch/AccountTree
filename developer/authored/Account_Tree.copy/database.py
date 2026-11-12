# -*- mode: python; coding: utf-8; python-indent-offset: 2; indent-tabs-mode: nil -*-
"""
database - the two kinds of Account-Tree database

  storage database   <tree dir>/database.sqlite3, identified by its database uuid.
                     The tree dir is <storage root>/home/Account-Tree, so on the
                     disk a system runs from it is /home/Account-Tree.
  system database    /var/lib/Account-Tree/system.sqlite3, identified by its system uuid

Each kind has its own schema, schema_storage.sql and schema_system.sql, and
both share the machinery here. Every change is made inside a transaction (see
Database.transaction), which records the statements it ran, a random number,
and a hash chained to the previous transaction's hash. The identity row holds
the series number and the hash of the last transaction, the head of the chain.

verify() checks the chain, and replays every recorded change into an empty
database built from the schema, so that a database whose tables were edited
outside a transaction, or whose record was altered, is found out.
"""

import os ,json ,sqlite3 ,hashlib ,secrets
from uuid import uuid4
from contextlib import contextmanager
from datetime import datetime ,timezone
from pathlib import Path

STANDARD = "5.0"
STANDARD_FOLLOWED = {"5.0"}


def genesis_hash(kind ,uuid_text) -> str:
  """
  The hash the first transaction follows. It is made from the database's
  kind and the uuid it was made with, so no two databases' chains can
  coincide, and a record cannot be moved from one database into another.
  """
  return hashlib.sha256(f"AccountTree {kind} {uuid_text}".encode("utf-8")).hexdigest()

HERE = Path(__file__).resolve().parent

# kind: (schema file, name of the identity column)
KIND = {
  "storage": (HERE / "schema_storage.sql" ,"database_uuid"),
  "system": (HERE / "schema_system.sql" ,"system_uuid"),
}

# Tables added to a kind's schema after databases of that kind were first made.
# Opening an existing database makes each one that is missing. A table made
# this way is empty, so the record's replay, built from the schema file, still
# matches; the statements are taken from the schema file itself, between the
# marker lines, so the two cannot drift apart.
def schema_added_text(kind) -> str:
  text = KIND[kind][0].read_text(encoding="utf-8")
  start = "CREATE TABLE IF NOT EXISTS"
  if start not in text:
    return ""
  tail = text[text.index(start):]
  # the added statements end where the next table without IF NOT EXISTS begins
  end = tail.find("\nCREATE TABLE ")
  while end != -1 and tail[end + 1:].startswith("CREATE TABLE IF NOT EXISTS"):
    end = tail.find("\nCREATE TABLE " ,end + 1)
  body = tail if end == -1 else tail[:end]
  return "\n".join(line for line in body.splitlines() if not line.startswith("--"))

SYSTEM_DIR = Path("/var/lib/Account-Tree")
SYSTEM_DATABASE_PATH = SYSTEM_DIR / "system.sqlite3"


def now() -> str:
  return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")

def storage_database_path(tree_dir) -> Path:
  return Path(tree_dir) / "database.sqlite3"

def account_data_dir(tree_dir) -> Path:
  return Path(tree_dir) / "account_data"

def transaction_hash(previous_hash ,series ,description ,change_text ,nonce) -> str:
  text = "\n".join([previous_hash ,str(series) ,description ,change_text ,nonce])
  return hashlib.sha256(text.encode("utf-8")).hexdigest()


class DatabaseError(Exception):
  pass


class Transaction:
  """
  Collects the statements of one transaction, so they can be recorded.
  """

  def __init__(self ,connection):
    self.connection = connection
    self.change = []

  def execute(self ,statement ,parameter=()):
    parameter = list(parameter)
    self.change.append([statement ,parameter])
    return self.connection.execute(statement ,parameter)


class Database:

  def __init__(self ,connection ,path ,kind):
    self.connection = connection
    self.path = path
    self.kind = kind
    self.schema_path ,self.uuid_column = KIND[kind]

  # ------------------------------------------------------------------------------
  # opening

  @classmethod
  def open(cls ,path ,kind):
    path = Path(path)
    if not path.is_file():
      raise DatabaseError(f"no {kind} database at {path}")
    connection = sqlite3.connect(str(path) ,isolation_level=None)
    connection.row_factory = sqlite3.Row
    database = cls(connection ,path ,kind)
    database.check_standard()
    added = schema_added_text(kind)
    if added:
      connection.executescript(added)
    return database

  @classmethod
  def make(cls ,path ,kind ,preview=False):
    """
    Make an empty database of the kind, with its identity row and a new uuid.
    The directory holding it must exist. In a preview the database is built in
    memory and discarded.
    """
    path = Path(path)
    if path.exists():
      raise DatabaseError(f"a database already exists at {path}")
    if preview:
      connection = sqlite3.connect(":memory:" ,isolation_level=None)
    else:
      connection = sqlite3.connect(str(path) ,isolation_level=None)
      os.chmod(path ,0o600)
    connection.row_factory = sqlite3.Row
    database = cls(connection ,path ,kind)
    connection.executescript(database.schema_path.read_text(encoding="utf-8"))
    uuid_text = str(uuid4())
    connection.execute("BEGIN")
    connection.execute(
      f"INSERT INTO identity({database.uuid_column} ,genesis_uuid ,series ,head_hash ,created) VALUES (? ,? ,0 ,? ,?)",
      (uuid_text ,uuid_text ,genesis_hash(kind ,uuid_text) ,now()))
    connection.execute("COMMIT")
    with database.transaction(f"make {kind} database") as t:
      t.execute("INSERT INTO standard(version) VALUES (?)" ,(STANDARD,))
    return database

  def check_standard(self):
    followed = {row["version"] for row in self.connection.execute("SELECT version FROM standard")}
    if not followed & STANDARD_FOLLOWED:
      raise DatabaseError(
        f"{self.path} follows standards {sorted(followed)}; this program follows {sorted(STANDARD_FOLLOWED)}")

  def close(self):
    self.connection.close()

  # ------------------------------------------------------------------------------
  # reading

  def identity(self):
    return self.connection.execute("SELECT * FROM identity").fetchone()

  def uuid(self) -> str:
    """
    The database uuid of a storage database, or the system uuid of a system database.
    """
    return self.identity()[self.uuid_column]

  def short(self) -> str:
    """
    The first eight digits of the uuid, for log lines.
    """
    return self.uuid()[:8]

  def head(self):
    """
    (series ,head hash) of the last transaction.
    """
    d = self.identity()
    return d["series"] ,d["head_hash"]

  def has_head(self ,series ,head_hash) -> bool:
    """
    Whether this database's record holds the transaction with that series
    number and hash: whether this database is, or descends from, the one that
    had that head.
    """
    if series == 0:
      return head_hash == genesis_hash(self.kind ,self.identity()["genesis_uuid"])
    return self.row("SELECT 1 FROM transaction_log WHERE series = ? AND hash = ?" ,(series ,head_hash)) is not None

  def last_change(self):
    return self.connection.execute("SELECT MAX(time) AS t FROM transaction_log").fetchone()["t"]

  def rows(self ,statement ,parameter=()):
    return self.connection.execute(statement ,list(parameter)).fetchall()

  def row(self ,statement ,parameter=()):
    return self.connection.execute(statement ,list(parameter)).fetchone()

  # ------------------------------------------------------------------------------
  # changing

  @contextmanager
  def transaction(self ,description ,preview=False):
    """
    with database.transaction("what this does") as t:
      t.execute(...)

    On leaving the block, the statements, a random number and the chained
    hash are recorded, and the head of the chain moves on, all in one SQLite
    transaction. In a preview, everything is rolled back instead.
    """
    connection = self.connection
    connection.execute("BEGIN IMMEDIATE")
    t = Transaction(connection)
    try:
      yield t
      if t.change:
        head = connection.execute("SELECT series ,head_hash FROM identity").fetchone()
        series = head["series"] + 1
        change_text = json.dumps(t.change ,ensure_ascii=False ,separators=(",",":"))
        nonce = secrets.token_hex(16)
        digest = transaction_hash(head["head_hash"] ,series ,description ,change_text ,nonce)
        connection.execute(
          "INSERT INTO transaction_log(series ,previous_hash ,nonce ,description ,change ,hash ,time)"
          " VALUES (? ,? ,? ,? ,? ,? ,?)",
          (series ,head["head_hash"] ,nonce ,description ,change_text ,digest ,now()))
        connection.execute("UPDATE identity SET series = ? ,head_hash = ?" ,(series ,digest))
      connection.execute("ROLLBACK" if preview else "COMMIT")
    except BaseException:
      connection.execute("ROLLBACK")
      raise

  # ------------------------------------------------------------------------------
  # checking

  def verify(self) -> list[str]:
    """
    Problems found, or an empty list.
    """
    problem = []
    identity = self.identity()
    previous = genesis_hash(self.kind ,identity["genesis_uuid"])
    expected_series = 1
    log = self.rows("SELECT * FROM transaction_log ORDER BY series")
    for entry in log:
      if entry["series"] != expected_series:
        problem.append(f"transaction {entry['series']}: expected series {expected_series}")
      if entry["previous_hash"] != previous:
        problem.append(f"transaction {entry['series']}: does not follow the transaction before it")
      digest = transaction_hash(entry["previous_hash"] ,entry["series"] ,entry["description"],
        entry["change"] ,entry["nonce"])
      if digest != entry["hash"]:
        problem.append(f"transaction {entry['series']}: hash does not match its content")
      previous = entry["hash"]
      expected_series = entry["series"] + 1
    last_series = log[-1]["series"] if log else 0
    if identity["series"] != last_series or identity["head_hash"] != previous:
      problem.append("the identity row's series and head do not match the last transaction")
    problem.extend(self.replay_problem(log))
    return problem

  def replay_problem(self ,log) -> list[str]:
    """
    Replay every recorded change into an empty database, and compare every
    table but the record itself. The identity row is compared without its
    series and head, which the chain bookkeeping sets.
    """
    replay = sqlite3.connect(":memory:")
    replay.executescript(self.schema_path.read_text(encoding="utf-8"))
    identity = self.identity()
    genesis_uuid = identity["genesis_uuid"]
    replay.execute(f"INSERT INTO identity({self.uuid_column} ,genesis_uuid ,series ,head_hash ,created) VALUES (? ,? ,0 ,? ,?)",
      (genesis_uuid ,genesis_uuid ,genesis_hash(self.kind ,genesis_uuid) ,identity["created"]))
    try:
      for entry in log:
        for statement ,parameter in json.loads(entry["change"]):
          replay.execute(statement ,parameter)
    except sqlite3.Error as error:
      return [f"replaying the record failed: {error}"]
    problem = []
    table_list = [r[0] for r in replay.execute(
      "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT IN ('identity' ,'transaction_log')"
      " AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    for table in table_list:
      here = sorted((tuple(r) for r in self.connection.execute(f"SELECT * FROM {table}")) ,key=repr)
      there = sorted((tuple(r) for r in replay.execute(f"SELECT * FROM {table}")) ,key=repr)
      if here != there:
        problem.append(f"table {table} differs from the replay of the record")
    # every column of identity but series and head_hash, which the chain sets
    column_list = [c[1] for c in replay.execute("PRAGMA table_info(identity)") if c[1] not in ("series" ,"head_hash")]
    column = "SELECT " + " ,".join(column_list) + " FROM identity"
    if tuple(self.connection.execute(column).fetchone()) != tuple(replay.execute(column).fetchone()):
      problem.append("table identity differs from the replay of the record")
    return problem
