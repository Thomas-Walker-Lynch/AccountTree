# -*- mode: python; coding: utf-8; python-indent-offset: 2; indent-tabs-mode: nil -*-
"""
system - the work functions for the system database

The system database, /var/lib/Account-Tree/system.sqlite3, is this system's own
record: its system uuid, the range of system uids Account-Tree gives out, the
system map of system accounts that are the same accounts as storage accounts,
every head of each storage database it has seen, and the limits on how many
child accounts each system account may have.
"""

import os
from pathlib import Path

import database
from database import Database
from common import host_name ,act
from system_account import system_account ,system_account__by_system_uid

DEFAULT__SYSTEM_UID__FIRST = 2000
DEFAULT__SYSTEM_UID__LAST = 59999
# the lowest database uid, the floor of the band storage databases count down in
DATABASE_UID__FLOOR = 1_879_048_192


def open_system_database(log):
  """
  This system's database, or None with an error logged when there is none.
  """
  if not database.SYSTEM_DATABASE__PATH.is_file():
    log.error("system" ,f"no system database at {database.SYSTEM_DATABASE__PATH}; run 'system make' first")
    return None
  return Database.open(database.SYSTEM_DATABASE__PATH ,"system")


def system__make(system_uid__first ,system_uid__last ,log ,preview):
  path = database.SYSTEM_DATABASE__PATH
  if system_uid__last >= DATABASE_UID__FLOOR:
    log.error("system uid range" ,f"the range reaches {DATABASE_UID__FLOOR}, where database uids begin"); return
  if system_uid__first > system_uid__last:
    log.error("system uid range" ,f"{system_uid__first} is above {system_uid__last}"); return
  if path.exists():
    db = Database.open(path ,"system")
    log.line("kept" ,str(path) ,f"a system database exists, system uuid {db.uuid()}")
    return
  if not database.SYSTEM_DIR.is_dir():
    if not act(log ,preview ,str(database.SYSTEM_DIR) ,os.makedirs ,database.SYSTEM_DIR ,0o700): return
    if not act(log ,preview ,str(database.SYSTEM_DIR) ,os.chmod ,database.SYSTEM_DIR ,0o700): return
    log.line("made" ,str(database.SYSTEM_DIR) ,"root only, mode 0700")
  db = Database.make(path ,"system" ,preview)
  with db.transaction("set system uid range" ,preview) as t:
    t.execute("INSERT INTO system_uid__range(first ,last) VALUES (? ,?)" ,(system_uid__first ,system_uid__last))
  # a system uuid is drawn at random; the one drawn in a preview is thrown away
  log.line("made" ,str(path) ,"a new system uuid" if preview else f"system uuid {db.uuid()}")
  log.line("recorded" ,"system uid range" ,f"{system_uid__first} to {system_uid__last}")

def system__show():
  path = database.SYSTEM_DATABASE__PATH
  db = Database.open(path ,"system")
  d = db.identity()
  r = db.row("SELECT * FROM system_uid__range")
  print(f"system    {host_name()}  system uuid {d['system_uuid']}")
  print(f"file      {path}")
  print(f"series    {d['series']}  head {d['head_hash'][:16]}")
  print(f"made      {d['created']}")
  print(f"changed   {db.last_change()}")
  print("standard  " + " ".join(s["version"] for s in db.rows("SELECT version FROM standard")))
  print(f"range     system uids {r['first']} to {r['last']}")
  print("system map")
  for m in db.rows("SELECT * FROM system_map ORDER BY state ,system_uid"):
    entry = system_account__by_system_uid(m["system_uid"])
    missing = m["state"] == "in use" and not (entry and entry.pw_name == m["system_account__name"])
    print(f"  {m['system_account__name']:<26}system uid {m['system_uid']:<7}{m['origin']:<8}{m['state']:<8}"
          f"database {m['database_uuid'][:8]} database uid {m['database_uid']}"
          + ("  (system account missing)" if missing else ""))
  print("storage heads last seen")
  for h in db.rows("SELECT h.* FROM storage_head__seen h WHERE h.id = (SELECT MAX(id) FROM storage_head__seen"
                   " WHERE database_uuid = h.database_uuid) ORDER BY h.seen"):
    print(f"  database {h['database_uuid'][:8]}  series {h['series']}  {h['event']} {h['seen']}"
          + (f"  ({h['continuity']})" if h["continuity"] else ""))
  device_row = db.rows("SELECT * FROM storage_device WHERE state = 'registered' ORDER BY storage_name")
  if device_row:
    print("removable storage")
    for r in device_row:
      plugged = Path(f"/run/Account-Tree/plugged/{r['storage_name']}.json").exists()
      print(f"  {r['storage_name']:<26}LUKS uuid {r['luks_uuid']}  database {r['database_uuid'][:8]}"
            f"  at {r['mount_dir']}" + ("  (plugged in)" if plugged else ""))
  limit_row = db.rows("SELECT * FROM account_limit ORDER BY system_uid")
  if limit_row:
    print("limits")
    for l in limit_row:
      entry = system_account__by_system_uid(l["system_uid"])
      who = entry.pw_name if entry else f"system uid {l['system_uid']}"
      print(f"  {who:<26}{l['maximum']}")
  problem = db.verify()
  print("verify    " + ("ok" if not problem else "; ".join(problem)))
  return 1 if problem else 0

def system__delete(log ,preview):
  """
  Delete the system database. Refused while its system map has any row in
  use: those system accounts would be left with no record, and their system
  uids could be given out again.
  """
  path = database.SYSTEM_DATABASE__PATH
  if not path.exists():
    log.line("kept" ,str(path) ,"no system database here; nothing to delete"); return
  db = Database.open(path ,"system")
  in_use = db.rows("SELECT * FROM system_map WHERE state = 'in use' ORDER BY system_account__name")
  if in_use:
    for m in in_use:
      log.error(str(path) ,f"{m['system_account__name']} is still the same account as database uid {m['database_uid']}"
                f" of database {m['database_uuid'][:8]}; run is-not-same-account, or storage delete, first")
    log.line("kept" ,str(path) ,"nothing was changed")
    return
  db.close()
  for entry in sorted(path.parent.iterdir()):
    if entry.name.startswith("system.sqlite3"):
      if not act(log ,preview ,str(entry) ,entry.unlink): return
      log.line("deleted" ,str(entry))
  if preview:
    log.line("deleted" ,str(path.parent) ,"when nothing else is in it")
  elif not any(path.parent.iterdir()):
    if act(log ,preview ,str(path.parent) ,path.parent.rmdir):
      log.line("deleted" ,str(path.parent))

def limit__set(system_account__name ,maximum ,log ,preview):
  """
  Set how many child accounts a system account may have below it.
  """
  if maximum < 0:
    log.error(system_account__name ,"a limit is 0 or more"); return
  db = open_system_database(log)
  if db is None: return
  entry = system_account(system_account__name)
  if entry is None:
    log.error(system_account__name ,"no such system account"); return
  old = db.row("SELECT maximum FROM account_limit WHERE system_uid = ?" ,(entry.pw_uid,))
  if old and old["maximum"] == maximum:
    log.line("kept" ,f"{system_account__name} limit {maximum}" ,"already set"); return
  with db.transaction(f"set limit of {system_account__name} to {maximum}" ,preview) as t:
    t.execute("INSERT INTO account_limit(system_uid ,maximum) VALUES (? ,?)"
              " ON CONFLICT(system_uid) DO UPDATE SET maximum = excluded.maximum" ,(entry.pw_uid ,maximum))
  was = f"was {old['maximum']}" if old else "was unset, which counts as 0"
  log.line("set" ,f"{system_account__name} limit {maximum}" ,was)
