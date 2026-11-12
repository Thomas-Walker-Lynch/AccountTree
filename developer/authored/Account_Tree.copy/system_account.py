# -*- mode: python; coding: utf-8; python-indent-offset: 2; indent-tabs-mode: nil -*-
"""
system_account - reading and changing the accounts of this system

Reading is done directly, through the C library's account functions, which
Python's pwd and grp modules call. Changing accounts is, for now, done by the
shadow suite's commands, useradd, userdel and groupdel, each wrapped in a
function named for what it does here.

In the future each change will be a direct function call: the files
/etc/passwd, /etc/shadow, /etc/group and /etc/gshadow edited under the C
library's lock, lckpwdf, by writing each new file beside the old one and
renaming it into place, so that a reader never sees a file half written.
"""

import pwd ,grp ,subprocess
from pathlib import Path

HOME = Path("/home")


class SystemAccountError(Exception):
  pass


# --------------------------------------------------------------------------------
# reading

def system_account(name):
  try:
    return pwd.getpwnam(name)
  except KeyError:
    return None

def system_account__by_system_uid(system_uid):
  try:
    return pwd.getpwuid(system_uid)
  except KeyError:
    return None

def system_account__list():
  return sorted(pwd.getpwall() ,key=lambda e: e.pw_name)

def system_group(name):
  try:
    return grp.getgrnam(name)
  except KeyError:
    return None

def system_group__by_system_gid(system_gid):
  try:
    return grp.getgrgid(system_gid)
  except KeyError:
    return None

def login_defs__value(key ,default):
  try:
    for line in Path("/etc/login.defs").read_text(encoding="utf-8").splitlines():
      field = line.split()
      if len(field) >= 2 and field[0] == key and field[1].isdigit():
        return int(field[1])
  except OSError:
    pass
  return default

def system_uid__min() -> int:
  """
  The lowest system uid of an account a person logs in to; below it are the
  accounts of the system's own services.
  """
  return login_defs__value("UID_MIN" ,1000)


# --------------------------------------------------------------------------------
# changing

def run_shadow(command):
  result = subprocess.run(command ,capture_output=True ,text=True)
  if result.returncode != 0:
    raise SystemAccountError(f"{' '.join(command)}: {result.stderr.strip() or 'exit ' + str(result.returncode)}")

def system_account__make(name ,system_uid):
  """
  Make a system account and its own group, both numbered system_uid, with its
  home at /home/<name>, which is not made: attach mounts the home there.

  In the future this will be a direct function call: lines added to
  /etc/passwd, /etc/shadow, /etc/group and /etc/gshadow under lckpwdf.
  """
  run_shadow(["useradd" ,"--badname" ,"-M" ,"-d" ,str(HOME / name) ,"-s" ,"/bin/bash",
              "-u" ,str(system_uid) ,"-U" ,name])

def system_account__delete(name):
  """
  Delete a system account, leaving its home alone: the home belongs to the
  storage it is mounted from.

  In the future this will be a direct function call: its lines removed from
  /etc/passwd and /etc/shadow under lckpwdf.
  """
  run_shadow(["userdel" ,name])

def system_group__delete(name):
  """
  Delete a system group.

  In the future this will be a direct function call: its lines removed from
  /etc/group and /etc/gshadow under lckpwdf.
  """
  run_shadow(["groupdel" ,name])
