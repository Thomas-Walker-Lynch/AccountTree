# -*- mode: python; coding: utf-8; python-indent-offset: 2; indent-tabs-mode: nil -*-
"""
same_account - the work functions that say which accounts are the same account

An account specifier names an account in one place:

  system <host name> <system account>    a system account on this system
  storage <tree dir> <storage account>   a storage account of the storage
                                         database in that tree dir

is-same-account is given two account specifiers. In 5.0 one names a system
account and the other a storage account; the correspondence is written into
the system map of the system database, which is the authority for which
storage accounts have system accounts here. The storage database records
only that this system has seen it.
"""

import os

import database
from database import Database
import kernel
from common import HOME ,act ,host_name ,valid_name ,tree_dir__problem ,delete_mount_point
from system import open_system_database
import system_account as sa


# --------------------------------------------------------------------------------
# opening the two sides

def open_sides(system_spec ,storage_spec ,log):
  """
  Check the two account specifiers and open both databases. Returns
  (system_db ,storage_db) or None with errors logged.
  """
  if system_spec["host"] != host_name():
    log.error(f"system {system_spec['host']}" ,f"this system is {host_name()}; only its own system accounts can be named")
    return None
  system_db = open_system_database(log)
  if system_db is None: return None
  problem = tree_dir__problem(storage_spec["tree_dir"])
  if problem:
    log.error(storage_spec["tree_dir"] ,problem); return None
  storage_db = Database.open(database.storage_database__path(storage_spec["tree_dir"]) ,"storage")
  return system_db ,storage_db

def mount_target(row):
  """
  Where the home of the system account in a system map row is mounted:
  /home/<system account>, which the system account's home in /etc/passwd must be.
  """
  return HOME / row["system_account__name"] ,sa.system_account__by_system_uid(row["system_uid"])

def is__mounted_from(target ,source):
  """
  Whether target is a mount of source: the root of a bind mount is the source
  directory itself, so the two have the same device and inode.
  """
  try:
    a ,b = os.stat(target) ,os.stat(source)
  except OSError:
    return False
  return kernel.is__mount_point(target) and (a.st_dev ,a.st_ino) == (b.st_dev ,b.st_ino)

def head_seen__record(system_db ,storage_db ,event ,continuity ,preview):
  """
  Record in the system database the storage database's head now.
  """
  series ,head_hash = storage_db.head()
  with system_db.transaction(f"{event} database {storage_db.short()} at series {series}" ,preview) as t:
    t.execute("INSERT INTO storage_head__seen(database_uuid ,series ,head_hash ,event ,continuity ,seen)"
              " VALUES (? ,? ,? ,? ,? ,?)" ,(storage_db.uuid() ,series ,head_hash ,event ,continuity ,database.now()))
  return series

def system_seen__record(storage_db ,system_uuid ,log ,preview):
  """
  Record in the storage database that this system has seen it now.
  """
  stamp = database.now()
  if storage_db.row("SELECT 1 FROM system_seen WHERE system_uuid = ?" ,(system_uuid,)):
    with storage_db.transaction(f"seen by system {system_uuid[:8]}" ,preview) as t:
      t.execute("UPDATE system_seen SET last = ? WHERE system_uuid = ?" ,(stamp ,system_uuid))
  else:
    with storage_db.transaction(f"first seen by system {system_uuid[:8]}" ,preview) as t:
      t.execute("INSERT INTO system_seen(system_uuid ,first ,last) VALUES (? ,? ,?)" ,(system_uuid ,stamp ,stamp))
    log.line("recorded" ,f"system {system_uuid[:8]}" ,"in the storage database's systems seen")


# --------------------------------------------------------------------------------
# is-same-account

def is__same_account(system_spec ,storage_spec ,make_system_account ,log ,preview):
  opened = open_sides(system_spec ,storage_spec ,log)
  if opened is None: return
  system_db ,storage_db = opened
  same_account__map(system_db ,storage_db ,system_spec["account"] ,storage_spec["account"] ,make_system_account ,log ,preview)

def same_account__map(system_db ,storage_db ,system_account__name ,storage_account__name ,make_system_account ,log ,preview):
  """
  Record in the system map that a system account and a storage account are the
  same account, adopting the system account when it exists, or making it when
  make_system_account is given. Returns True when the row was made, or would be.
  """
  storage_account = storage_db.row("SELECT * FROM account WHERE name = ?" ,(storage_account__name,))
  if storage_account is None:
    log.error(storage_account__name ,f"no such storage account in database {storage_db.short()}"); return False
  if storage_account["state"] != "active":
    log.error(storage_account__name ,f"the storage account is {storage_account['state']}"); return False
  database_uuid = storage_db.uuid()

  existing = system_db.row("SELECT * FROM system_map WHERE database_uuid = ? AND database_uid = ? AND state = 'in use'",
                           (database_uuid ,storage_account["database_uid"]))
  if existing:
    if existing["system_account__name"] == system_account__name:
      log.line("kept" ,f"{system_account__name} is {storage_account__name}" ,"already the same account"); return True
    log.error(storage_account__name ,f"already the same account as system account {existing['system_account__name']}"); return False

  entry = sa.system_account(system_account__name)
  if entry is not None:
    if make_system_account:
      log.error(system_account__name ,"the system account exists; make-system-account is for one that does not"); return False
    if entry.pw_uid < sa.system_uid__min():
      log.error(system_account__name ,f"system uid {entry.pw_uid} is below UID_MIN {sa.system_uid__min()}, an account of the system's own services"); return False
    taken = system_db.row("SELECT * FROM system_map WHERE system_uid = ? AND state = 'in use'" ,(entry.pw_uid,))
    if taken:
      log.error(system_account__name ,f"already the same account as database uid {taken['database_uid']} of database {taken['database_uuid'][:8]}"); return False
    if entry.pw_dir != str(HOME / system_account__name):
      log.error(system_account__name ,f"its home is {entry.pw_dir}; Account-Tree mounts a home only at /home/<system account>"); return False
    system_uid ,system_gid ,origin = entry.pw_uid ,entry.pw_gid ,"adopted"
    log.line("adopted" ,system_account__name ,f"existing system account, system uid {system_uid}")
  else:
    if not make_system_account:
      log.error(system_account__name ,"no such system account; give the keyword make-system-account to make it"); return False
    problem = valid_name(system_account__name)
    if problem:
      log.error(system_account__name ,problem); return False
    home = HOME / system_account__name
    if kernel.is__mount_point(home) or (home.exists() and (not home.is_dir() or any(home.iterdir()))):
      log.error(str(home) ,"already holds something; a system account made here would take it as its home"); return False
    system_uid = free__system_uid(system_db)
    if system_uid is None:
      log.error(system_account__name ,"no free system uid in this system's range"); return False
    if not act(log ,preview ,system_account__name ,sa.system_account__make ,system_account__name ,system_uid): return False
    system_gid = system_uid if preview else sa.system_account(system_account__name).pw_gid
    origin = "made"
    log.line("made system account" ,system_account__name ,f"system uid {system_uid}, gid {system_gid}")

  system_seen__record(storage_db ,system_db.uuid() ,log ,preview)
  with system_db.transaction(f"{system_account__name} is the same account as {storage_account__name}"
                             f" of database {storage_db.short()}" ,preview) as t:
    t.execute("INSERT INTO system_map(system_uid ,system_gid ,system_account__name ,database_uuid ,database_uid"
              " ,origin ,state ,mapped) VALUES (? ,? ,? ,? ,? ,? ,'in use' ,?)",
              (system_uid ,system_gid ,system_account__name ,database_uuid ,storage_account["database_uid"] ,origin ,database.now()))
  head_seen__record(system_db ,storage_db ,"mapped" ,None ,preview)
  log.line("mapped" ,f"{system_account__name} is {storage_account__name}",
           f"system uid {system_uid} is database uid {storage_account['database_uid']} of database {storage_db.short()}")
  return True

def free__system_uid(system_db):
  """
  The lowest system uid in the range that no system account and no system
  group has, and that the system map has never held, in use or retired.
  """
  r = system_db.row("SELECT * FROM system_uid__range")
  held = {m["system_uid"] for m in system_db.rows("SELECT system_uid FROM system_map")}
  for c in range(r["first"] ,r["last"] + 1):
    if c in held or sa.system_account__by_system_uid(c) is not None or sa.system_group__by_system_gid(c) is not None:
      continue
    return c
  return None


# --------------------------------------------------------------------------------
# is-not-same-account

def is__not_same_account(system_spec ,storage_spec ,log ,preview):
  opened = open_sides(system_spec ,storage_spec ,log)
  if opened is None: return
  system_db ,storage_db = opened
  system_account__name ,storage_account__name = system_spec["account"] ,storage_spec["account"]
  storage_account = storage_db.row("SELECT * FROM account WHERE name = ?" ,(storage_account__name,))
  if storage_account is None:
    log.error(storage_account__name ,f"no such storage account in database {storage_db.short()}"); return
  row = system_db.row("SELECT * FROM system_map WHERE database_uuid = ? AND database_uid = ? AND state = 'in use'",
                      (storage_db.uuid() ,storage_account["database_uid"]))
  if row is None:
    log.line("kept" ,storage_account__name ,"not the same account as any system account here; nothing to undo"); return
  if row["system_account__name"] != system_account__name:
    log.error(storage_account__name ,f"is the same account as {row['system_account__name']}, not {system_account__name}"); return
  if row["system_uid"] in kernel.session__system_uid__set():
    log.error(system_account__name ,"has a login session; nothing was changed"); return
  same_account__end(row ,system_db ,storage_spec["tree_dir"] ,storage_account__name ,log ,preview)

def same_account__end(row ,system_db ,tree_dir ,storage_account__name ,log ,preview):
  """
  Undo one row of the system map: unmount the home if it is mounted from this
  storage, delete the mount point attach made, delete the system account if
  Account-Tree made it, and retire the row. Returns False when it had to stop.
  """
  name = row["system_account__name"]
  target ,entry = mount_target(row)
  source = database.account_data__dir(tree_dir) / storage_account__name
  if is__mounted_from(target ,source):
    if not act(log ,preview ,str(target) ,kernel.home__unmount ,target):
      log.line("stopped" ,name ,"the home could not be unmounted; nothing further was changed")
      return False
    log.line("unmounted" ,str(target))
  delete_mount_point(target ,log ,preview)
  if row["origin"] == "made" and entry is not None and entry.pw_name == name:
    group_entry = sa.system_group(name)
    if not act(log ,preview ,name ,sa.system_account__delete ,name): return False
    log.line("deleted system account" ,name ,f"system uid {row['system_uid']}")
    if group_entry is not None and group_entry.gr_gid == row["system_gid"] and not group_entry.gr_mem:
      if preview or sa.system_group(name) is not None:
        if not act(log ,preview ,name ,sa.system_group__delete ,name): return False
      log.line("deleted system group" ,name ,f"gid {row['system_gid']}")
  elif row["origin"] == "adopted":
    log.line("kept" ,f"system account {name}" ,"it existed before Account-Tree adopted it")
  with system_db.transaction(f"{name} is no longer the same account as database uid {row['database_uid']}"
                             f" of database {row['database_uuid'][:8]}" ,preview) as t:
    t.execute("UPDATE system_map SET state = 'retired' ,retired = ? WHERE id = ?" ,(database.now() ,row["id"]))
  log.line("retired" ,f"system uid {row['system_uid']}" ,"in the system map; it is not given out again")
  return True


# --------------------------------------------------------------------------------
# show and suggest

def same_account__show(storage_spec):
  """
  The system accounts that are the same accounts as storage accounts; given a
  storage, its storage accounts that are not, and the login accounts that are not.
  """
  system_db = Database.open(database.SYSTEM_DATABASE__PATH ,"system")
  storage_db = None
  if storage_spec:
    storage_db = Database.open(database.storage_database__path(storage_spec["tree_dir"]) ,"storage")
  in_use = system_db.rows("SELECT * FROM system_map WHERE state = 'in use' ORDER BY system_account__name")
  if storage_db:
    in_use = [m for m in in_use if m["database_uuid"] == storage_db.uuid()]
  print(f"same accounts on system {host_name()}")
  for m in in_use:
    other = f"database uid {m['database_uid']}"
    if storage_db:
      a = storage_db.row("SELECT name FROM account WHERE database_uid = ?" ,(m["database_uid"],))
      other = a["name"] if a else "(no such storage account)"
    print(f"  system {m['system_account__name']:<26} storage {m['database_uuid'][:8]} {other}  ({m['origin']})")
  if storage_db:
    mapped = {m["database_uid"] for m in in_use}
    print(f"storage accounts of database {storage_db.short()} that are not the same account as any system account here")
    for a in storage_db.rows("SELECT * FROM account WHERE state = 'active' ORDER BY name"):
      if a["database_uid"] not in mapped:
        print(f"  {a['name']}")
  mapped_name = {m["system_account__name"] for m in system_db.rows("SELECT system_account__name FROM system_map WHERE state = 'in use'")}
  print("system accounts of persons here that are not the same account as any storage account")
  for entry in sa.system_account__list():
    if sa.system_uid__min() <= entry.pw_uid < 60000 and entry.pw_name not in mapped_name:
      print(f"  {entry.pw_name}")
  return 0

def same_account__suggest(storage_spec):
  """
  Print is-same-account commands for the storage accounts that are not yet the
  same account as any system account, proposed by matching names. Changes nothing.
  """
  system_db = Database.open(database.SYSTEM_DATABASE__PATH ,"system")
  tree_dir = storage_spec["tree_dir"]
  storage_db = Database.open(database.storage_database__path(tree_dir) ,"storage")
  mapped = {m["database_uid"] for m in system_db.rows(
    "SELECT database_uid FROM system_map WHERE state = 'in use' AND database_uuid = ?" ,(storage_db.uuid(),))}
  taken = {m["system_uid"] for m in system_db.rows("SELECT system_uid FROM system_map WHERE state = 'in use'")}
  print("# proposed by matching names; read each line before running it")
  for a in storage_db.rows("SELECT * FROM account WHERE state = 'active' ORDER BY name"):
    if a["database_uid"] in mapped: continue
    entry = sa.system_account(a["name"])
    line = f"Account-Tree_admin is-same-account system {host_name()} '{a['name']}' storage {tree_dir} '{a['name']}'"
    if entry is None:
      print(line + " make-system-account")
    elif entry.pw_uid in taken or entry.pw_uid < sa.system_uid__min():
      print(f"# {a['name']}: the system account of that name cannot be adopted (system uid {entry.pw_uid})")
    else:
      print(line + f"    # adopts the existing system account, system uid {entry.pw_uid}")
  return 0
