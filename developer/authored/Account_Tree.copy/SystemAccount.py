# -*- mode: python; coding: utf-8; python-indent-offset: 2; indent-tabs-mode: nil -*-
"""
SystemAccount - reading and changing the accounts of this system

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

DIRPA__HOME = Path("/home")
FPA__SHADOW = Path("/etc/shadow")


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

def list__system_account():
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

def list__supplementary_group(name):
  """The names of the groups the account is a member of besides its own."""
  return sorted(g.gr_name for g in grp.getgrall() if name in g.gr_mem)

def shadow__field(name):
  """
  The fields of the account's line in /etc/shadow after its name: password,
  last change, minimum, maximum, warning, inactive, expire and reserved, as
  text. None when it has no line.
  """
  for line in FPA__SHADOW.read_text(encoding="utf-8").splitlines():
    field = line.split(":")
    if field[0] == name and len(field) == 9:
      return field[1:]
  return None

def system_account__setting(name):
  """
  What a system account has that its storage does not carry: its password and
  password aging from /etc/shadow, its comment and shell from /etc/passwd, and
  its supplementary groups. These are kept while the account is unplugged, so
  that it can be made again as it was.
  """
  entry = system_account(name)
  if entry is None:
    return None
  shadow = shadow__field(name) or ["!" ,"" ,"" ,"" ,"" ,"" ,"" ,""]
  return {"comment": entry.pw_gecos ,"shell": entry.pw_shell ,"password": shadow[0],
          "aging": dict(zip(("last_change" ,"minimum" ,"maximum" ,"warning" ,"inactive" ,"expire") ,shadow[1:7])),
          "list__supplementary_group": list__supplementary_group(name)}

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

def system_account__make(name ,system_uid ,setting=None):
  """
  Make a system account and its own group, both numbered system_uid, with its
  home at /home/<name>, which is not made: attach mounts the home there.
  Given setting, as system_account__setting returns it, the account is made
  with that password, aging, comment, shell and those supplementary groups;
  the groups named must exist.

  In the future this will be a direct function call: lines added to
  /etc/passwd, /etc/shadow, /etc/group and /etc/gshadow under lckpwdf.
  """
  command = ["useradd" ,"--badname" ,"-M" ,"-d" ,str(DIRPA__HOME / name) ,"-u" ,str(system_uid) ,"-U"]
  if setting is None:
    run_shadow(command + ["-s" ,"/bin/bash" ,name]); return
  command += ["-s" ,setting["shell"] or "/bin/bash" ,"-c" ,setting["comment"] ,"-p" ,setting["password"]]
  if setting["list__supplementary_group"]:
    command += ["-G" ,",".join(setting["list__supplementary_group"])]
  run_shadow(command + [name])
  # useradd stamps the password as changed today; put back what it was. An
  # empty field is written as chage's -1 where chage allows it; an empty last
  # change cannot be written, so useradd's date stands for it
  aging = setting["aging"]
  option = {"last_change": "-d" ,"minimum": "-m" ,"maximum": "-M" ,"warning": "-W" ,"inactive": "-I" ,"expire": "-E"}
  chage = []
  for key ,flag in option.items():
    value = aging.get(key ,"")
    if value != "":
      chage += [flag ,value]
    elif key in ("maximum" ,"inactive" ,"expire"):
      chage += [flag ,"-1"]
  if chage:
    run_shadow(["chage"] + chage + [name])

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
