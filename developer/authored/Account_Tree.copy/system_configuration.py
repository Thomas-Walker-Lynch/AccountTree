# -*- mode: python; coding: utf-8; python-indent-offset: 2; indent-tabs-mode: nil -*-
"""
system_configuration - the systemd units and udev rules Account-Tree places

  configure(log ,preview)          write the units and the rules, then have
                                   systemd and udev read them again
  rule__write(system_db ,log ,preview)
                                   write the rules from the registered storage

Each file begins with MARK. A file Account-Tree writes is replaced only when it
carries the mark; a file of the same name without it belongs to someone else,
and is reported and left alone. This is the rule the installer is to follow
for all system configuration.

How a plug reaches Account-Tree. udev runs the rules when a device appears.

  A FIDO2 key, a hidraw device that systemd's 60-fido-id.rules marks
  ID_SECURITY_TOKEN=1, is tagged for systemd, so that systemd makes a device
  unit for it, dev-hidrawN.device, active while the key is plugged in. It also
  wants Account-Tree_key-arrive.service, which plugs any registered storage
  that arrived before its key.

  A registered storage's LUKS2 partition, matched by its LUKS uuid, wants
  Account-Tree_plug@<kernel name>.service, such as Account-Tree_plug@sdb1. The
  service is bound to the partition's device unit: it starts when the drive
  arrives, running storage plug, and systemd stops it when the drive goes,
  running storage unplug. The partition, and the unlocked device
  Account-Tree_<storage name>, are marked UDISKS_IGNORE, so that the desktop's
  file manager neither asks for a passphrase nor offers its own eject, which
  cannot end the accounts' processes first.

  When a key unlocks a storage, plug starts Account-Tree_key@hidrawN.service,
  bound to the key's device unit; pulling the key out stops it, and its
  ExecStop unplugs every storage that key unlocked.

A rule that tags a device must tag it on every event but remove. A change
event that left the tag off would make systemd take the device unit away, and
with it every service bound to it, which would unplug the storage.
"""

import os ,stat ,subprocess
from pathlib import Path

from common import act
from storage_plug import registration__list ,run ,run_logged ,program

MARK = "# made by Account-Tree; Account-Tree_admin system configure replaces this file"
UNIT_DIR = Path("/etc/systemd/system")
RULE_PATH = Path("/etc/udev/rules.d/90-Account-Tree.rules")
TRUE_PATH = "/usr/bin/true"


def admin_path():
  """
  The Account-Tree_admin the units are to run: the one running now.
  """
  return Path(os.path.realpath(os.path.join(os.path.dirname(os.path.realpath(__file__)) ,"Account-Tree_admin")))

def path_changer(path):
  """
  Why someone other than root can change the program at path, or the
  directories it is in, or None. The units run it under root.
  """
  p = Path(path)
  for q in [p] + list(p.parents):
    try:
      st = q.stat()
    except OSError as error:
      return f"{q}: {error}"
    if st.st_uid != 0:
      return f"{q} is owned by uid {st.st_uid}"
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not (q.is_dir() and st.st_mode & stat.S_ISVTX):
      return f"{q} can be written by its group or by others"
  return None

def quoted(path):
  return '"' + str(path).replace("%" ,"%%") + '"'

def unit_text():
  admin = quoted(admin_path())
  return {
    "Account-Tree_plug@.service": f"""{MARK}
[Unit]
Description=Account-Tree storage on /dev/%i
BindsTo=dev-%i.device
After=dev-%i.device

[Service]
Type=oneshot
RemainAfterExit=yes
# waits for a touch of the key
TimeoutStartSec=180
ExecStart={admin} storage plug /dev/%i
ExecStop={admin} storage unplug /dev/%i
""",
    "Account-Tree_key@.service": f"""{MARK}
[Unit]
Description=Account-Tree key on /dev/%i
BindsTo=dev-%i.device
After=dev-%i.device

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart={TRUE_PATH}
ExecStop={admin} storage unplug key /dev/%i
""",
    "Account-Tree_key-arrive.service": f"""{MARK}
[Unit]
Description=Account-Tree plug of storage waiting for a key

[Service]
Type=oneshot
TimeoutStartSec=300
ExecStart={admin} storage plug all
""",
  }

def rule_text(system_db):
  line = [MARK ,"" ,
    "# a FIDO2 key: a systemd device unit while it is plugged in, and a plug of any storage waiting for it",
    'SUBSYSTEM=="hidraw", ACTION!="remove", ENV{ID_SECURITY_TOKEN}=="1", TAG+="systemd",'
    ' ENV{SYSTEMD_WANTS}+="Account-Tree_key-arrive.service"',
    "" ,
    "# the device a storage is unlocked as: kept out of the file manager",
    'SUBSYSTEM=="block", ENV{DM_NAME}=="Account-Tree_*", ENV{UDISKS_IGNORE}="1"',
  ]
  for r in registration__list(system_db):
    line += ["" ,f"# storage {r['storage_name']}, mounted at {r['mount_dir']}",
      f'SUBSYSTEM=="block", ACTION!="remove", ENV{{ID_FS_UUID}}=="{r["luks_uuid"]}", ENV{{UDISKS_IGNORE}}="1",'
      ' TAG+="systemd", ENV{SYSTEMD_WANTS}+="Account-Tree_plug@%k.service"']
  return "\n".join(line) + "\n"

def file__write(path ,text ,log ,preview):
  """
  Write a configuration file that carries MARK. Returns True when the file
  holds text afterwards, or would.
  """
  if path.exists():
    try:
      old = path.read_text(encoding="utf-8")
    except OSError as error:
      log.error(str(path) ,str(error)); return False
    if not old.startswith(MARK):
      log.error(str(path) ,"exists and was not made by Account-Tree; it is left as it is"); return False
    if old == text:
      log.line("kept" ,str(path) ,"unchanged"); return True
  def write():
    temporary = path.with_name(f".{path.name}.Account-Tree")
    temporary.write_text(text ,encoding="utf-8")
    os.chmod(temporary ,0o644)
    os.replace(temporary ,path)
  if not act(log ,preview ,str(path) ,write): return False
  log.line("written" ,str(path))
  return True

def rule__write(system_db ,log ,preview):
  if not RULE_PATH.parent.is_dir():
    log.line("skipped" ,str(RULE_PATH) ,"this system has no /etc/udev/rules.d"); return False
  if not file__write(RULE_PATH ,rule_text(system_db) ,log ,preview): return False
  if program("udevadm") and run_logged(log ,preview ,"udev" ,["udevadm" ,"control" ,"--reload"]):
    log.line("reloaded" ,"udev rules")
  return True

def configure(log ,preview):
  from system import open_system_database
  system_db = open_system_database(log)
  if system_db is None: return
  admin = admin_path()
  problem = path_changer(admin)
  if problem:
    log.line("warning" ,str(admin) ,f"the units run this program under root, but {problem}; until Account-Tree is"
             " installed where only root can change it, whoever can change it can act as root")
  if not UNIT_DIR.is_dir():
    log.error(str(UNIT_DIR) ,"no such directory; is this system run by systemd?"); return
  for name ,text in unit_text().items():
    file__write(UNIT_DIR / name ,text ,log ,preview)
  if run_logged(log ,preview ,"systemd" ,["systemctl" ,"daemon-reload"]):
    log.line("reloaded" ,"systemd units")
  if not rule__write(system_db ,log ,preview): return
  # tag the keys plugged in now, so that systemd has their device units
  if program("udevadm") and run_logged(log ,preview ,"udev" ,["udevadm" ,"trigger" ,"--action=change" ,"--subsystem-match=hidraw"]):
    log.line("reloaded" ,"the keys plugged in now" ,"udev ran its rules for them again")
