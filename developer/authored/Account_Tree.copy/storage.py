# -*- mode: python; coding: utf-8; python-indent-offset: 2; indent-tabs-mode: nil -*-
"""
storage - the work functions for storage databases

A storage is a persistent storage device that keeps accounts' data: the disk
inside the machine, or a drive plugged in. Its tree dir is
<storage root>/home/Account-Tree, which holds the storage database and
account-data, the storage accounts' homes. On the disk a system runs from, the
tree dir is /home/Account-Tree.

Which storage accounts are the same accounts as system accounts here is
recorded in the system map of the system database, and attach mounts exactly
the accounts it lists.
"""

import os ,shutil
from uuid import uuid4
from pathlib import Path

import database
from database import Database
import kernel
from common import HOME ,SEPARATOR ,BRANCH_MARK ,act ,valid_name ,tree_dir__problem ,host_name
from system import open_system_database ,DATABASE_UID__FLOOR
import same_account

# A storage database gives out database uids counting down from
# DATABASE_UID__FIRST, while systems give out system uids counting up from low
# numbers. See "Limits of uids" in the design document.
DATABASE_UID__FIRST = 1_999_999_999


def open_storage_database(tree_dir ,log):
  problem = tree_dir__problem(tree_dir)
  if problem:
    log.error(str(tree_dir) ,problem); return None
  return Database.open(database.storage_database__path(tree_dir) ,"storage")

def system_map__in_use(system_db ,storage_db):
  return system_db.rows("SELECT * FROM system_map WHERE database_uuid = ? AND state = 'in use' ORDER BY system_account__name",
                        (storage_db.uuid(),))


# --------------------------------------------------------------------------------
# storage make, show, delete

def storage__make(tree_dir ,log ,preview):
  problem = tree_dir__problem(tree_dir)
  if problem:
    log.error(str(tree_dir) ,problem); return
  tree_dir = Path(tree_dir)
  home = tree_dir.parent
  root = home.parent
  if not root.is_dir():
    log.error(str(root) ,"the storage root is not a directory"); return
  path = database.storage_database__path(tree_dir)
  if path.exists():
    db = Database.open(path ,"storage")
    log.line("kept" ,str(path) ,f"a storage database exists, database uuid {db.uuid()}")
  else:
    for directory ,mode in ((home ,0o755) ,(tree_dir ,0o700)):
      if not directory.is_dir():
        if not act(log ,preview ,str(directory) ,os.mkdir ,directory ,mode): return
        if not act(log ,preview ,str(directory) ,os.chmod ,directory ,mode): return
        log.line("made" ,str(directory) ,f"mode {mode:04o}")
    db = Database.make(path ,"storage" ,preview)
    # a database uuid is drawn at random; the one drawn in a preview is thrown away
    log.line("made" ,str(path) ,"a new database uuid" if preview else f"database uuid {db.uuid()}")
  account_data = database.account_data__dir(tree_dir)
  if account_data.is_dir():
    log.line("kept" ,str(account_data))
  else:
    if not act(log ,preview ,str(account_data) ,os.mkdir ,account_data ,0o700): return
    if not act(log ,preview ,str(account_data) ,os.chmod ,account_data ,0o700): return
    log.line("made" ,str(account_data) ,"root only, mode 0700")

def storage__show(tree_dir):
  problem = tree_dir__problem(tree_dir)
  if problem:
    print(f"Error: {problem}"); return 1
  db = Database.open(database.storage_database__path(tree_dir) ,"storage")
  d = db.identity()
  print(f"storage   database uuid {d['database_uuid']}")
  if d["branch_of"]:
    print(f"branch    of database {d['branch_of']}, after series {d['branch__after_series']}")
  print(f"file      {db.path}")
  print(f"series    {d['series']}  head {d['head_hash'][:16]}")
  print(f"made      {d['created']}")
  print(f"changed   {db.last_change()}")
  print("standard  " + " ".join(r["version"] for r in db.rows("SELECT version FROM standard")))
  mapped = {}
  if database.SYSTEM_DATABASE__PATH.is_file():
    system_db = Database.open(database.SYSTEM_DATABASE__PATH ,"system")
    mapped = {m["database_uid"]: m for m in system_map__in_use(system_db ,db)}
  print("storage accounts")
  for a in db.rows("SELECT * FROM account ORDER BY name"):
    m = mapped.get(a["database_uid"])
    here = f"system account {m['system_account__name']}, system uid {m['system_uid']}" if m else "no system account here"
    print(f"  {a['name']:<28}database uid {a['database_uid']:<12}{a['state']:<10}{here}")
  for n in db.rows("SELECT * FROM system_seen ORDER BY first"):
    print(f"seen by   system {n['system_uuid']}  first {n['first']}  last {n['last']}")
  problem = db.verify()
  print("verify    " + ("ok" if not problem else "; ".join(problem)))
  return 1 if problem else 0

def storage__delete(tree_dir ,delete_account_data ,forget_other_systems ,log ,preview):
  """
  Undo on this system what Account-Tree did for the storage database in
  tree_dir, then delete the tree dir. Every check is made before anything is
  changed; a check that fails stops the command with nothing changed.
  """
  problem = tree_dir__problem(tree_dir)
  if problem:
    log.error(str(tree_dir) ,problem); return
  tree_dir = Path(tree_dir)
  account_data = database.account_data__dir(tree_dir)
  path = database.storage_database__path(tree_dir)
  if not path.is_file():
    log.line("kept" ,str(tree_dir) ,"no storage database here; nothing to delete"); return
  db = Database.open(path ,"storage")
  system_db = Database.open(database.SYSTEM_DATABASE__PATH ,"system") if database.SYSTEM_DATABASE__PATH.is_file() else None
  this_system__uuid = system_db.uuid() if system_db else None
  in_use = system_map__in_use(system_db ,db) if system_db else []
  account_name = {a["database_uid"]: a["name"] for a in db.rows("SELECT database_uid ,name FROM account")}

  # ---- checks
  problem = []
  if not forget_other_systems:
    for n in db.rows("SELECT * FROM system_seen WHERE system_uuid IS NOT ?" ,(this_system__uuid,)):
      problem.append(f"system {n['system_uuid'][:8]} has system accounts for storage accounts of this database, and its"
                     " system map would name a database that no longer exists; give the keyword forget-other-systems"
                     " to delete anyway")
  in_session = kernel.session__system_uid__set()
  for m in in_use:
    if m["system_uid"] in in_session:
      problem.append(f"{m['system_account__name']} has a login session")
  for target in kernel.mount_point__list():
    if target.startswith(str(tree_dir) + "/"):
      problem.append(f"{target} is a mount point inside {tree_dir}")
  for entry in sorted(tree_dir.iterdir()):
    if entry.name != "account-data" and not entry.name.startswith("database.sqlite3"):
      problem.append(f"{entry} is not Account-Tree's")
  holding_file = []
  if account_data.is_dir():
    for entry in sorted(account_data.iterdir()):
      if entry.name not in account_name.values():
        problem.append(f"{entry} is not the home of a storage account of this database")
      elif entry.is_dir() and any(entry.iterdir()):
        holding_file.append(entry.name)
  if holding_file and not delete_account_data:
    problem.append("these homes hold files: " + ", ".join(holding_file) + "; give the keyword delete-account-data to delete them too")
  if problem:
    for p in problem: log.error(str(tree_dir) ,p)
    log.line("kept" ,str(tree_dir) ,"nothing was changed")
    return

  # ---- this system's system accounts for its storage accounts
  for m in in_use:
    if not same_account.same_account__end(m ,system_db ,tree_dir ,account_name.get(m["database_uid"] ,"?") ,log ,preview):
      log.line("stopped" ,str(tree_dir) ,"nothing further was changed"); return

  # ---- files
  if account_data.is_dir():
    home_count = sum(1 for _ in account_data.iterdir())
    if not act(log ,preview ,str(account_data) ,shutil.rmtree ,account_data): return
    log.line("deleted" ,str(account_data) ,f"{home_count} home" + ("" if home_count == 1 else "s"))
  short ,database_uuid = db.short() ,db.uuid()
  db.close()
  for entry in sorted(tree_dir.iterdir()):
    if entry.name.startswith("database.sqlite3"):
      if not act(log ,preview ,str(entry) ,entry.unlink): return
      log.line("deleted" ,str(entry) ,f"database {short}" if entry.name == "database.sqlite3" else None)
  if not act(log ,preview ,str(tree_dir) ,tree_dir.rmdir): return
  log.line("deleted" ,str(tree_dir))

  # ---- its record as a removable storage
  if system_db is not None:
    import storage_plug
    for r in system_db.rows("SELECT * FROM storage_device WHERE database_uuid = ? AND state = 'registered'" ,(database_uuid,)):
      storage_plug.unregister(r["storage_name"] ,log ,preview)


# --------------------------------------------------------------------------------
# account make

def account__make(tree_dir ,name ,database_uid ,parent ,make_system_account ,log ,preview):
  """
  Make a storage account in the storage database in tree_dir, with its home in
  account-data. Its parent is the account whose name its own begins with,
  before the last '·', or the one given; an account with no '·' in its name
  is a child of root. With make_system_account, also make a system account of
  the same name and record that the two are the same account.
  """
  problem = valid_name(name)
  if problem:
    log.error(name ,problem); return
  db = open_storage_database(tree_dir ,log)
  if db is None: return
  if db.row("SELECT 1 FROM account WHERE name = ?" ,(name,)):
    log.line("kept" ,f"storage account {name}" ,"already in this database"); return
  if parent is None and SEPARATOR in name:
    parent = name.rsplit(SEPARATOR ,1)[0]
  if parent is not None and not name.startswith(parent + SEPARATOR):
    log.error(name ,f"the name of a child account of {parent} begins with '{parent}{SEPARATOR}'"); return
  parent_row = db.row("SELECT database_uid FROM account WHERE name = ?" ,(parent,)) if parent else None
  parent__database_uid = parent_row["database_uid"] if parent_row else None

  if database_uid is None:
    # the next database uid down from the lowest given out in the band; one
    # kept with the keyword uid lies outside the band and does not move the count
    lowest = db.row("SELECT MIN(database_uid) AS m FROM database_uid WHERE database_uid >= ?",
                    (DATABASE_UID__FLOOR,))["m"]
    database_uid = DATABASE_UID__FIRST if lowest is None else lowest - 1
    if database_uid < DATABASE_UID__FLOOR:
      log.error(name ,"this database has given out every database uid in its band"); return
  elif db.row("SELECT 1 FROM database_uid WHERE database_uid = ?" ,(database_uid,)):
    log.error(name ,f"database uid {database_uid} has been given out before"); return

  home = database.account_data__dir(tree_dir) / name
  if home.exists() and home.stat().st_uid != database_uid:
    log.error(str(home) ,f"exists, owned by uid {home.stat().st_uid}, not database uid {database_uid}"); return

  stamp = database.now()
  with db.transaction(f"make storage account {name}" ,preview) as t:
    t.execute("INSERT INTO database_uid(database_uid ,state) VALUES (? ,'in use')" ,(database_uid,))
    t.execute("INSERT INTO account(database_uid ,name ,parent__database_uid ,parent_name ,state ,created ,changed)"
      " VALUES (? ,? ,? ,? ,'active' ,? ,?)" ,(database_uid ,name ,parent__database_uid ,parent ,stamp ,stamp))
  log.line("made" ,f"storage account {name}" ,f"database uid {database_uid}" + (f", child of {parent}" if parent else ", child of root"))
  if home.exists():
    log.line("kept" ,str(home) ,"existing home, owned by its database uid")
  else:
    if not act(log ,preview ,str(home) ,os.mkdir ,home ,0o700): return
    if not act(log ,preview ,str(home) ,os.chown ,home ,database_uid ,database_uid): return
    if not act(log ,preview ,str(home) ,os.chmod ,home ,0o700): return
    log.line("made" ,str(home) ,f"owned by database uid {database_uid}, mode 0700")

  if make_system_account:
    if preview:
      # the storage account was not made, so the system map could not name it;
      # say what would be done
      log.line("made system account" ,name ,"with the next free system uid in this system's range")
      log.line("mapped" ,f"{name} is {name}" ,"in the system map")
      return
    system_db = open_system_database(log)
    if system_db is None: return
    same_account.same_account__map(system_db ,db ,name ,name ,True ,log ,preview)


# --------------------------------------------------------------------------------
# continuity and branches

def continuity(system_db ,db):
  """
  How the storage database's record relates to the heads this system has seen
  of it. Returns (kind ,seen row):
    new         this system has seen no head of it
    forward     its record holds the last head seen: it has moved on since
    backwards   its head is one seen earlier: an older copy
    sideways    its record holds an earlier head seen, not the last one: a branch,
                or an older copy whose head this system never saw
    unrelated   its record holds no head this system has seen
  The seen row is the last head seen that its record holds, if any.
  """
  seen = system_db.rows("SELECT * FROM storage_head__seen WHERE database_uuid = ? ORDER BY id" ,(db.uuid(),))
  if not seen:
    return "new" ,None
  if db.has__head(seen[-1]["series"] ,seen[-1]["head_hash"]):
    return "forward" ,seen[-1]
  series ,head_hash = db.head()
  for s in reversed(seen):
    if (s["series"] ,s["head_hash"]) == (series ,head_hash):
      return "backwards" ,s
  for s in reversed(seen):
    if db.has__head(s["series"] ,s["head_hash"]):
      return "sideways" ,s
  return "unrelated" ,None

def storage__branch(system_db ,db ,tree_dir ,log ,preview):
  """
  Make the storage database a branch: give it a new database uuid, record the
  database it came from and the head they parted after, and make a system
  account <name>~<eight hex digits> for each storage account that has a system
  account on the line it came from. Both lines can then be attached at once.
  """
  parent_uuid = db.uuid()
  kind ,seen = continuity(system_db ,db)
  after_series ,after_head = (seen["series"] ,seen["head_hash"]) if seen else (0 ,database.genesis_hash("storage" ,db.identity()["genesis_uuid"]))
  in_use = system_map__in_use(system_db ,db)
  new_uuid = str(uuid4())
  with db.transaction(f"branch from database {parent_uuid[:8]} after series {after_series}" ,preview) as t:
    t.execute("UPDATE identity SET database_uuid = ? ,branch_of = ? ,branch__after_series = ? ,branch__after_head = ?",
              (new_uuid ,parent_uuid ,after_series ,after_head))
  # the branch is named by the hash of the transaction that made it, which no
  # other branch has; in a preview it is not known
  suffix = "<8 hex>" if preview else db.head()[1][:8]
  log.line("branched" ,f"database {parent_uuid[:8]}",
           f"{'a new database uuid' if preview else 'now database ' + new_uuid[:8]}, after series {after_series}, {kind}")
  for m in in_use:
    a = db.row("SELECT name FROM account WHERE database_uid = ?" ,(m["database_uid"],))
    if a is None: continue
    name = f"{m['system_account__name']}{BRANCH_MARK}{suffix}"
    if preview:
      log.line("made system account" ,name ,"with the next free system uid in this system's range")
      log.line("mapped" ,f"{name} is {a['name']}" ,"of the branch")
      continue
    same_account.same_account__map(system_db ,db ,name ,a["name"] ,True ,log ,preview)


# --------------------------------------------------------------------------------
# attach and detach

def attach(tree_dir ,branch ,log ,preview):
  """
  Mount the home of each storage account that the system map makes the same
  account as a system account here, at /home/<system account>.

  The storage's record is compared with the heads this system has seen of it.
  A storage gone back to an older copy, or sideways to a branch, is attached
  with a warning: no one is kept from working. Given branch, the storage is
  first made a branch of its own, with its own system accounts, so that it and
  the line it came from can both be attached. A storage whose homes are
  already mounted from another copy of it is refused, with that advice.
  """
  system_db = open_system_database(log)
  if system_db is None: return
  db = open_storage_database(tree_dir ,log)
  if db is None: return
  problem = db.verify()
  if problem:
    for p in problem: log.error(str(db.path) ,p)
    return
  log.line("found" ,f"database {db.short()}" ,f"series {db.head()[0]}, chain verified")

  if branch:
    storage__branch(system_db ,db ,tree_dir ,log ,preview)
    if preview:
      log.line("mounted" ,"the branch's homes" ,"at /home/<system account>~<8 hex>")
      return

  in_use = system_map__in_use(system_db ,db)
  if not in_use:
    log.line("skipped" ,str(tree_dir) ,"no storage account of this database is the same account as a system account here")
    return

  # the same database attached twice would mount two homes at one place
  for m in in_use:
    a = db.row("SELECT name FROM account WHERE database_uid = ?" ,(m["database_uid"],))
    target ,_ = same_account.mount_target(m)
    source = database.account_data__dir(tree_dir) / (a["name"] if a else "")
    if kernel.is__mount_point(target) and not same_account.is__mounted_from(target ,source):
      log.error(str(target) ,"already mounted from another copy of this database, or from something else;"
                " detach that, or attach this storage with the keyword branch. Nothing was mounted.")
      return

  kind ,seen = continuity(system_db ,db)
  series = db.head()[0]
  if kind == "new":
    log.line("found" ,f"database {db.short()}" ,"not attached here before")
  elif kind == "forward":
    log.line("found" ,f"database {db.short()}",
             f"holds the head last seen here, series {seen['series']}; {series - seen['series']} transactions since")
  elif kind == "backwards":
    log.line("warning" ,f"database {db.short()}",
             f"its head, series {series}, is the one seen here at {seen['seen']}, and another state of this"
             " database has been attached here since: this is an older copy, or the other line of a branch."
             " Merging the two lines is by hand")
  elif kind == "sideways":
    log.line("warning" ,f"database {db.short()}",
             f"went sideways: it holds the head seen here at series {seen['series']}, but not the last one;"
             " it is a branch, or an older copy. Merging the two lines is by hand")
  else:
    log.line("warning" ,f"database {db.short()}" ,"its record holds no head seen here; it parted before this system first saw it")

  for m in in_use:
    attach_account(tree_dir ,db ,m ,log ,preview)
  mapped = {m["database_uid"] for m in in_use}
  for a in db.rows("SELECT * FROM account WHERE state = 'active' ORDER BY name"):
    if a["database_uid"] not in mapped:
      log.line("skipped" ,f"storage account {a['name']}" ,"not the same account as any system account here")
  same_account.system_seen__record(db ,system_db.uuid() ,log ,preview)
  seen_series = same_account.head_seen__record(system_db ,db ,"attached" ,kind ,preview)
  log.line("recorded" ,f"database {db.short()} attached" ,f"head at series {seen_series} remembered here")

def attach_account(tree_dir ,db ,m ,log ,preview):
  a = db.row("SELECT * FROM account WHERE database_uid = ?" ,(m["database_uid"],))
  if a is None or a["state"] != "active":
    log.error(m["system_account__name"] ,f"the system map names database uid {m['database_uid']},"
              " which is not an active storage account of this database"); return
  target ,entry = same_account.mount_target(m)
  if entry is None or entry.pw_name != m["system_account__name"]:
    log.error(m["system_account__name"] ,f"no system account {m['system_account__name']} with system uid {m['system_uid']};"
              " it is to be made again"); return
  if entry.pw_dir != str(target):
    log.error(m["system_account__name"] ,f"its home is {entry.pw_dir}; Account-Tree mounts a home only at {target}"); return
  source = database.account_data__dir(tree_dir) / a["name"]
  if not source.is_dir():
    log.error(str(source) ,"no such directory"); return
  if kernel.is__mount_point(target):
    log.line("kept" ,str(target) ,"already mounted"); return
  if target.exists():
    if not target.is_dir():
      log.error(str(target) ,"exists and is not a directory"); return
    if any(target.iterdir()):
      log.error(str(target) ,"not empty and not a mount point; nothing is mounted over it"); return
  else:
    if not act(log ,preview ,str(target) ,os.mkdir ,target ,0o000): return
    if not act(log ,preview ,str(target) ,os.chmod ,target ,0o000): return
    if not act(log ,preview ,str(target) ,kernel.immutable__set ,target ,True): return
    log.line("made" ,str(target) ,"mount point, root, mode 0000, immutable")
  database_uid = m["database_uid"]
  if not act(log ,preview ,str(target) ,kernel.home__mount ,source ,target ,database_uid ,m["system_uid"] ,m["system_gid"]):
    return
  how = ("bind mount, the database uid and system uid are the same" if database_uid == m["system_uid"] == m["system_gid"]
         else f"idmapped, database uid {database_uid} to system uid {m['system_uid']}")
  log.line("mounted" ,str(target) ,f"{how}; nosuid, nodev")

def detach(tree_dir ,log ,preview):
  system_db = open_system_database(log)
  if system_db is None: return
  db = open_storage_database(tree_dir ,log)
  if db is None: return
  in_session = kernel.session__system_uid__set()
  in_use = system_map__in_use(system_db ,db)
  failed = False
  for m in in_use:
    a = db.row("SELECT name FROM account WHERE database_uid = ?" ,(m["database_uid"],))
    target ,_ = same_account.mount_target(m)
    source = database.account_data__dir(tree_dir) / (a["name"] if a else "")
    if not same_account.is__mounted_from(target ,source):
      log.line("kept" ,str(target) ,"not mounted from this storage"); continue
    if m["system_uid"] in in_session:
      log.error(m["system_account__name"] ,"has a login session; not detached"); failed = True; continue
    if act(log ,preview ,str(target) ,kernel.home__unmount ,target):
      log.line("unmounted" ,str(target))
    else:
      failed = True
  if in_use and not failed:
    same_account.system_seen__record(db ,system_db.uuid() ,log ,preview)
    seen_series = same_account.head_seen__record(system_db ,db ,"detached" ,None ,preview)
    log.line("recorded" ,f"database {db.short()} detached" ,f"head at series {seen_series} remembered here")
