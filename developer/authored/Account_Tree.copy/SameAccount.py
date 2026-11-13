# -*- mode: python; coding: utf-8; python-indent-offset: 2; indent-tabs-mode: nil -*-
"""
SameAccount - the work functions that say which accounts are the same account

An account specifier names an account in one place:

  system <host name> <system account>       a system account on this system
  storage <dirp_storage> <storage account>   a storage account of the storage
                                             whose root is <dirp_storage>

is-same-account is given two account specifiers. In 5.0 one names a system
account and the other a storage account; the correspondence is written into
the system map of the system database, which is the authority for which
storage accounts have system accounts here. The storage database records
only that this system has seen it.

A row of the system map is the introduction of a storage account to this
system, which only an administrator makes. A removable storage that is
unplugged keeps its introductions, but the system accounts Account-Tree made
for them are deleted (unplug), and made again, with the next free system
uids, when it is plugged in (replug). An adopted system account is kept.
"""

import os
from pathlib import Path

import Database
import Kernel
import AccountsService
from Common import DIRPA__HOME ,act ,host_name ,valid_name ,dirp__storage__problem ,dirpa__Account_Tree ,delete_mount_point
from System import open_system_database
import SystemAccount as sa

# The places on this system, outside the storage, where an account's processes
# leave files, searched at unplug for files the freed system uid owns. A file
# left anywhere else would belong to whoever is given the uid next; unplug logs
# none of those, as finding them means reading the whole file system.
LIST__DIRPA__LOCAL = (Path("/tmp") ,Path("/var/tmp") ,Path("/dev/shm"))
# files named for the account rather than owned by it
LIST__DIRPA__BY_NAME = (Path("/var/spool/mail") ,Path("/var/spool/cron") ,Path("/var/lib/systemd/linger"))
DIRPA__ACCOUNTS_SERVICE__ICON = (Path("/var/lib/AccountsService/icons") ,Path("/var/lib/accountsservice/icons"))
# The fields of SystemAccount.system_account__setting's aging, and the columns
# of system_account__saved that hold them.
SAVED__AGING_COLUMN = {"last_change": "password__last_change" ,"minimum": "password__minimum_day",
                       "maximum": "password__maximum_day" ,"warning": "password__warning_day",
                       "inactive": "password__inactive_day" ,"expire": "account__expire"}

def saved__write(t ,row ,setting):
  """In transaction t, keep the setting of the system account of row."""
  key = (row["database_uuid"] ,row["database_uid"])
  column = list(SAVED__AGING_COLUMN.values())
  value = [int(setting["aging"][k]) if setting["aging"].get(k ,"") != "" else None for k in SAVED__AGING_COLUMN]
  t.execute("INSERT INTO system_account__saved(database_uuid ,database_uid ,comment ,shell ,password ," + " ,".join(column) + ")"
            " VALUES (?" + " ,?" * (4 + len(column)) + ")" ,key + (setting["comment"] ,setting["shell"] ,setting["password"]) + tuple(value))
  for g in setting["list__supplementary_group"]:
    t.execute("INSERT INTO system_account__saved_group(database_uuid ,database_uid ,group_name) VALUES (? ,? ,?)" ,key + (g,))

def saved__read(system_db ,row):
  """The setting kept for the system account of row, or None."""
  key = (row["database_uuid"] ,row["database_uid"])
  r = system_db.row("SELECT * FROM system_account__saved WHERE database_uuid = ? AND database_uid = ?" ,key)
  if r is None:
    return None
  return {"comment": r["comment"] ,"shell": r["shell"] ,"password": r["password"],
          "aging": {k: ("" if r[c] is None else str(r[c])) for k ,c in SAVED__AGING_COLUMN.items()},
          "list__supplementary_group": [g["group_name"] for g in system_db.rows(
            "SELECT group_name FROM system_account__saved_group WHERE database_uuid = ? AND database_uid = ? ORDER BY group_name" ,key)]}

def saved__delete(t ,row):
  """In transaction t, delete what was kept for the system account of row."""
  key = (row["database_uuid"] ,row["database_uid"])
  t.execute("DELETE FROM system_account__saved WHERE database_uuid = ? AND database_uid = ?" ,key)
  t.execute("DELETE FROM system_account__saved_group WHERE database_uuid = ? AND database_uid = ?" ,key)


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
  problem = dirp__storage__problem(storage_spec["dirp__storage"])
  if problem:
    log.error(storage_spec["dirp__storage"] ,problem); return None
  storage_db = Database.Database.open(Database.fpa__storage_database(dirpa__Account_Tree(storage_spec["dirp__storage"])) ,"storage")
  return system_db ,storage_db

def mount_target(row):
  """
  Where the home of the system account in a system map row is mounted:
  /home/<system account>, which the system account's home in /etc/passwd must be.
  """
  return DIRPA__HOME / row["system_account__name"] ,sa.system_account__by_system_uid(row["system_uid"])

def is__mounted_from(dirp__target ,dirp__source):
  """
  Whether target is a mount of source: the root of a bind mount is the source
  directory itself, so the two have the same device and inode.
  """
  try:
    a ,b = os.stat(dirp__target) ,os.stat(dirp__source)
  except OSError:
    return False
  return Kernel.is__mount_point(dirp__target) and (a.st_dev ,a.st_ino) == (b.st_dev ,b.st_ino)

def head_seen__record(system_db ,storage_db ,event ,continuity ,preview):
  """
  Record in the system database the storage database's head now.
  """
  series ,head_hash = storage_db.head()
  with system_db.transaction(f"{event} database {storage_db.short()} at series {series}" ,preview) as t:
    t.execute("INSERT INTO storage_head__seen(database_uuid ,series ,head_hash ,event ,continuity ,seen)"
              " VALUES (? ,? ,? ,? ,? ,?)" ,(storage_db.uuid() ,series ,head_hash ,event ,continuity ,Database.now()))
  return series

def system_seen__record(storage_db ,system_uuid ,log ,preview):
  """
  Record in the storage database that this system has seen it now.
  """
  stamp = Database.now()
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

  existing = system_db.row("SELECT * FROM system_map WHERE database_uuid = ? AND database_uid = ? AND state IN ('in use' ,'unplugged')",
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
    if entry.pw_dir != str(DIRPA__HOME / system_account__name):
      log.error(system_account__name ,f"its home is {entry.pw_dir}; Account-Tree mounts a home only at /home/<system account>"); return False
    system_uid ,system_gid ,origin = entry.pw_uid ,entry.pw_gid ,"adopted"
    log.line("adopted" ,system_account__name ,f"existing system account, system uid {system_uid}")
  else:
    if not make_system_account:
      log.error(system_account__name ,"no such system account; give the keyword make-system-account to make it"); return False
    problem = valid_name(system_account__name)
    if problem:
      log.error(system_account__name ,problem); return False
    dirpa__home = DIRPA__HOME / system_account__name
    if Kernel.is__mount_point(dirpa__home) or (dirpa__home.exists() and (not dirpa__home.is_dir() or any(dirpa__home.iterdir()))):
      log.error(str(dirpa__home) ,"already holds something; a system account made here would take it as its home"); return False
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
              (system_uid ,system_gid ,system_account__name ,database_uuid ,storage_account["database_uid"] ,origin ,Database.now()))
  head_seen__record(system_db ,storage_db ,"mapped" ,None ,preview)
  log.line("mapped" ,f"{system_account__name} is {storage_account__name}",
           f"system uid {system_uid} is database uid {storage_account['database_uid']} of database {storage_db.short()}")
  return True

def free__system_uid(system_db ,set__promised=frozenset()):
  """
  The lowest system uid in the range that no system account and no system
  group has, and that no row of the system map holds, in use or retired. A
  row unplugged holds none: its uid was freed, with the files it owned in the
  places searched. A uid in set__promised, one a preview has already said
  it would give out, is passed over too.
  """
  r = system_db.row("SELECT * FROM system_uid__range")
  held = {m["system_uid"] for m in system_db.rows("SELECT system_uid FROM system_map WHERE system_uid IS NOT NULL")}
  for c in range(r["first"] ,r["last"] + 1):
    if c in held or c in set__promised or sa.system_account__by_system_uid(c) is not None or sa.system_group__by_system_gid(c) is not None:
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
  row = system_db.row("SELECT * FROM system_map WHERE database_uuid = ? AND database_uid = ? AND state IN ('in use' ,'unplugged')",
                      (storage_db.uuid() ,storage_account["database_uid"]))
  if row is None:
    log.line("kept" ,storage_account__name ,"not the same account as any system account here; nothing to undo"); return
  if row["system_account__name"] != system_account__name:
    log.error(storage_account__name ,f"is the same account as {row['system_account__name']}, not {system_account__name}"); return
  if row["system_uid"] in Kernel.set__session__system_uid():
    log.error(system_account__name ,"has a login session; nothing was changed"); return
  same_account__end(row ,system_db ,dirpa__Account_Tree(storage_spec["dirp__storage"]) ,storage_account__name ,log ,preview)

def made_system_account__delete(row ,log ,preview):
  """
  Delete a system account Account-Tree made, and its own group when no other
  account is a member of it. Returns False when it could not.
  """
  name = row["system_account__name"]
  group_entry = sa.system_group(name)
  if not act(log ,preview ,name ,sa.system_account__delete ,name): return False
  log.line("deleted system account" ,name ,f"system uid {row['system_uid']}")
  if group_entry is not None and group_entry.gr_gid == row["system_gid"] and not group_entry.gr_mem:
    if preview or sa.system_group(name) is not None:
      if not act(log ,preview ,name ,sa.system_group__delete ,name): return False
    log.line("deleted system group" ,name ,f"gid {row['system_gid']}")
  return True

def same_account__end(row ,system_db ,dirp__Account_Tree ,storage_account__name ,log ,preview):
  """
  Undo one row of the system map: unmount the home if it is mounted from this
  storage, delete the mount point attach made, delete the system account if
  Account-Tree made it, and retire the row. Returns False when it had to stop.
  """
  name = row["system_account__name"]
  if row["state"] == "unplugged":
    with system_db.transaction(f"{name} is no longer the same account as database uid {row['database_uid']}"
                               f" of database {row['database_uuid'][:8]}" ,preview) as t:
      t.execute("UPDATE system_map SET state = 'retired' ,retired = ? WHERE id = ?" ,(Database.now() ,row["id"]))
      saved__delete(t ,row)
    log.line("retired" ,f"{name}" ,"in the system map; it had no system account here, its storage being unplugged")
    return True
  dirpa__target ,entry = mount_target(row)
  dirp__source = Database.dirp__account_data(dirp__Account_Tree) / storage_account__name
  if is__mounted_from(dirpa__target ,dirp__source):
    if not act(log ,preview ,str(dirpa__target) ,Kernel.home__unmount ,dirpa__target):
      log.line("stopped" ,name ,"the home could not be unmounted; nothing further was changed")
      return False
    log.line("unmounted" ,str(dirpa__target))
  delete_mount_point(dirpa__target ,log ,preview)
  if row["origin"] == "made" and entry is not None and entry.pw_name == name:
    if not made_system_account__delete(row ,log ,preview): return False
  elif row["origin"] == "adopted":
    log.line("kept" ,f"system account {name}" ,"it existed before Account-Tree adopted it")
  with system_db.transaction(f"{name} is no longer the same account as database uid {row['database_uid']}"
                             f" of database {row['database_uuid'][:8]}" ,preview) as t:
    t.execute("UPDATE system_map SET state = 'retired' ,retired = ? WHERE id = ?" ,(Database.now() ,row["id"]))
  log.line("retired" ,f"system uid {row['system_uid']}" ,"in the system map; it is not given out again")
  return True


# --------------------------------------------------------------------------------
# show and suggest

def same_account__show(storage_spec):
  """
  The system accounts that are the same accounts as storage accounts; given a
  storage, its storage accounts that are not, and the login accounts that are not.
  """
  system_db = Database.Database.open(Database.FPA__SYSTEM_DATABASE ,"system")
  storage_db = None
  if storage_spec:
    problem = dirp__storage__problem(storage_spec["dirp__storage"])
    if problem:
      print(f"Error: {problem}"); return 1
    storage_db = Database.Database.open(Database.fpa__storage_database(dirpa__Account_Tree(storage_spec["dirp__storage"])) ,"storage")
  in_use = system_db.rows("SELECT * FROM system_map WHERE state IN ('in use' ,'unplugged') ORDER BY system_account__name")
  if storage_db:
    in_use = [m for m in in_use if m["database_uuid"] == storage_db.uuid()]
  print(f"same accounts on system {host_name()}")
  for m in in_use:
    other = f"database uid {m['database_uid']}"
    if storage_db:
      a = storage_db.row("SELECT name FROM account WHERE database_uid = ?" ,(m["database_uid"],))
      other = a["name"] if a else "(no such storage account)"
    how = m["origin"] + (", unplugged: no system account while its storage is out" if m["state"] == "unplugged" else "")
    print(f"  system {m['system_account__name']:<26} storage {m['database_uuid'][:8]} {other}  ({how})")
  if storage_db:
    mapped = {m["database_uid"] for m in in_use}
    print(f"storage accounts of database {storage_db.short()} that are not the same account as any system account here")
    for a in storage_db.rows("SELECT * FROM account WHERE state = 'active' ORDER BY name"):
      if a["database_uid"] not in mapped:
        print(f"  {a['name']}")
  mapped_name = {m["system_account__name"] for m in system_db.rows("SELECT system_account__name FROM system_map WHERE state IN ('in use' ,'unplugged')")}
  print("system accounts of persons here that are not the same account as any storage account")
  for entry in sa.list__system_account():
    if sa.system_uid__min() <= entry.pw_uid < 60000 and entry.pw_name not in mapped_name:
      print(f"  {entry.pw_name}")
  return 0

def same_account__suggest(storage_spec):
  """
  Print is-same-account commands for the storage accounts that are not yet the
  same account as any system account, proposed by matching names. Changes nothing.
  """
  system_db = Database.Database.open(Database.FPA__SYSTEM_DATABASE ,"system")
  problem = dirp__storage__problem(storage_spec["dirp__storage"])
  if problem:
    print(f"Error: {problem}"); return 1
  dirp__storage = storage_spec["dirp__storage"]
  storage_db = Database.Database.open(Database.fpa__storage_database(dirpa__Account_Tree(dirp__storage)) ,"storage")
  mapped = {m["database_uid"] for m in system_db.rows(
    "SELECT database_uid FROM system_map WHERE state IN ('in use' ,'unplugged') AND database_uuid = ?" ,(storage_db.uuid(),))}
  taken = {m["system_uid"] for m in system_db.rows("SELECT system_uid FROM system_map WHERE state = 'in use'")}
  print("# proposed by matching names; read each line before running it")
  for a in storage_db.rows("SELECT * FROM account WHERE state = 'active' ORDER BY name"):
    if a["database_uid"] in mapped: continue
    entry = sa.system_account(a["name"])
    line = f"Account-Tree_admin is-same-account system {host_name()} '{a['name']}' storage {dirp__storage} '{a['name']}'"
    if entry is None:
      print(line + " make-system-account")
    elif entry.pw_uid in taken or entry.pw_uid < sa.system_uid__min():
      print(f"# {a['name']}: the system account of that name cannot be adopted (system uid {entry.pw_uid})")
    else:
      print(line + f"    # adopts the existing system account, system uid {entry.pw_uid}")
  return 0


# --------------------------------------------------------------------------------
# unplug and replug

def local_file__delete(system_uid ,name ,log ,preview):
  """
  Delete what an account left on this system outside its storage: the files
  and directories owned by system_uid in LIST__DIRPA__LOCAL, and the files
  named for the account in LIST__DIRPA__BY_NAME. A directory it owns that
  holds another account's file is given to root instead, mode 0700, and
  logged. File systems mounted below a place are not entered.
  """
  count = 0
  def walk(dirpa ,dev):
    nonlocal count
    try:
      list__entry = list(os.scandir(dirpa))
    except OSError:
      return
    for e in list__entry:
      try:
        st = e.stat(follow_symlinks=False)
      except OSError:
        continue
      if st.st_dev != dev: continue
      if e.is_dir(follow_symlinks=False):
        walk(e.path ,dev)
        if st.st_uid == system_uid:
          if preview:
            count += 1; continue
          try:
            os.rmdir(e.path); count += 1
          except OSError:
            os.chown(e.path ,0 ,0 ,follow_symlinks=False); os.chmod(e.path ,0o700)
            log.line("kept" ,e.path ,"it holds files of other accounts; given to root, mode 0700")
      elif st.st_uid == system_uid:
        if not preview:
          try:
            os.unlink(e.path)
          except OSError as error:
            log.line("skipped" ,e.path ,f"not deleted: {error.strerror}"); continue
        count += 1
  for dirpa in LIST__DIRPA__LOCAL:
    if dirpa.is_dir():
      walk(str(dirpa) ,os.lstat(dirpa).st_dev)
  for dirpa in LIST__DIRPA__BY_NAME + DIRPA__ACCOUNTS_SERVICE__ICON:
    fpa = dirpa / name
    if fpa.exists() or fpa.is_symlink():
      if not act(log ,preview ,str(fpa) ,fpa.unlink): continue
      count += 1
  d = AccountsService.dirp__user()
  if d is not None and (d / name).exists():
    if act(log ,preview ,str(d / name) ,(d / name).unlink):
      count += 1
  if count:
    log.line("deleted" ,f"{count} file" + ("" if count == 1 else "s") ,f"that {name} left on this system")

def same_account__unplug(system_db ,row ,log ,preview):
  """
  For a row in use whose system account Account-Tree made: keep what the
  system account has that its storage does not carry, delete the system
  account, its group, its mount point and the files it left on this system,
  and mark the row unplugged, freeing its system uid. Run by unplug after the
  account's processes have ended and its home is unmounted. Returns False
  when it could not.
  """
  name = row["system_account__name"]
  entry = sa.system_account(name)
  if entry is None or entry.pw_uid != row["system_uid"]:
    log.line("warning" ,name ,f"no system account {name} with system uid {row['system_uid']}; the row is unplugged with nothing kept")
    setting = None
  else:
    setting = sa.system_account__setting(name)
    if not made_system_account__delete(row ,log ,preview): return False
  delete_mount_point(DIRPA__HOME / name ,log ,preview)
  local_file__delete(row["system_uid"] ,name ,log ,preview)
  with system_db.transaction(f"{name} unplugged: system uid {row['system_uid']} freed" ,preview) as t:
    t.execute("UPDATE system_map SET state = 'unplugged' ,system_uid = NULL ,system_gid = NULL WHERE id = ?" ,(row["id"],))
    saved__delete(t ,row)
    if setting: saved__write(t ,row ,setting)
  log.line("unplugged" ,name ,f"system uid {row['system_uid']} freed; made again when its storage is plugged in")
  return True

def same_account__replug(system_db ,row ,log ,preview ,set__promised=None):
  """
  For an unplugged row: make its system account again, with the next free
  system uid and what was kept of it, and put the row back in use. Run by
  attach before it mounts the homes. In a preview the uid it would give is
  added to set__promised, so that the next is not said to get the same one.
  Returns False when it could not.
  """
  name = row["system_account__name"]
  if sa.system_account(name) is not None:
    log.error(name ,f"a system account {name} exists here that Account-Tree did not make; it is not attached."
              " Rename that account, or run is-not-same-account for this one"); return False
  dirpa__home = DIRPA__HOME / name
  if Kernel.is__mount_point(dirpa__home) or (dirpa__home.exists() and (not dirpa__home.is_dir() or any(dirpa__home.iterdir()))):
    log.error(str(dirpa__home) ,"already holds something; the system account is not made again"); return False
  system_uid = free__system_uid(system_db ,set__promised or frozenset())
  if system_uid is None:
    log.error(name ,"no free system uid in this system's range"); return False
  if set__promised is not None: set__promised.add(system_uid)
  setting = saved__read(system_db ,row)
  if setting:
    list__group = setting["list__supplementary_group"]
    list__missing = [g for g in list__group if sa.system_group(g) is None]
    if list__missing:
      log.line("warning" ,name ,"no longer in " + ", ".join(list__missing) + ", which this system no longer has")
    setting = dict(setting ,list__supplementary_group=[g for g in list__group if g not in list__missing])
  if not act(log ,preview ,name ,sa.system_account__make ,name ,system_uid ,setting): return False
  system_gid = system_uid if preview else sa.system_account(name).pw_gid
  with system_db.transaction(f"{name} plugged in: system uid {system_uid}" ,preview) as t:
    t.execute("UPDATE system_map SET state = 'in use' ,system_uid = ? ,system_gid = ? WHERE id = ?",
              (system_uid ,system_gid ,row["id"]))
    saved__delete(t ,row)
  kept = "its password, shell and groups as before" if setting else "nothing was kept of it"
  log.line("made system account" ,name ,f"system uid {system_uid}, gid {system_gid}; {kept}")
  return True
