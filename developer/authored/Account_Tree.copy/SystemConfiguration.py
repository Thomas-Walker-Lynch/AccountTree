# -*- mode: python; coding: utf-8; python-indent-offset: 2; indent-tabs-mode: nil -*-
"""
SystemConfiguration - the systemd units and udev rules Account-Tree places

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

from Common import act
from StoragePlug import list__registration ,run ,run_logged ,program

MARK = "# made by Account-Tree; Account-Tree_admin system configure replaces this file"
DIRPA__UNIT = Path("/etc/systemd/system")
FPA__RULE = Path("/etc/udev/rules.d/90-Account-Tree.rules")
FPA__TRUE = "/usr/bin/true"


def fpa__admin():
  """
  The Account-Tree_admin the units are to run: the one running now.
  """
  return Path(os.path.realpath(os.path.join(os.path.dirname(os.path.realpath(__file__)) ,"Account-Tree_admin")))

def path_changer(filep__checked):
  """
  Why someone other than root can change the program at path, or the
  directories it is in, or None. The units run it under root.
  """
  p = Path(filep__checked)
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

def quoted(filep__quoted):
  return '"' + str(filep__quoted).replace("%" ,"%%") + '"'

def unit_text():
  admin = quoted(fpa__admin())
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
ExecStart={FPA__TRUE}
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
  for r in list__registration(system_db):
    line += ["" ,f"# storage {r['storage_name']}, mounted at {r['dirpa__mount']}",
      f'SUBSYSTEM=="block", ACTION!="remove", ENV{{ID_FS_UUID}}=="{r["luks_uuid"]}", ENV{{UDISKS_IGNORE}}="1",'
      ' TAG+="systemd", ENV{SYSTEMD_WANTS}+="Account-Tree_plug@%k.service"']
  return "\n".join(line) + "\n"

def file__write(filep__written ,text ,log ,preview):
  """
  Write a configuration file that carries MARK. Returns True when the file
  holds text afterwards, or would.
  """
  if filep__written.exists():
    try:
      old = filep__written.read_text(encoding="utf-8")
    except OSError as error:
      log.error(str(filep__written) ,str(error)); return False
    if not old.startswith(MARK):
      log.error(str(filep__written) ,"exists and was not made by Account-Tree; it is left as it is"); return False
    if old == text:
      log.line("kept" ,str(filep__written) ,"unchanged"); return True
  def write():
    temporary = filep__written.with_name(f".{filep__written.name}.Account-Tree")
    temporary.write_text(text ,encoding="utf-8")
    os.chmod(temporary ,0o644)
    os.replace(temporary ,filep__written)
  if not act(log ,preview ,str(filep__written) ,write): return False
  log.line("written" ,str(filep__written))
  return True

def rule__write(system_db ,log ,preview):
  if not FPA__RULE.parent.is_dir():
    log.line("skipped" ,str(FPA__RULE) ,"this system has no /etc/udev/rules.d"); return False
  if not file__write(FPA__RULE ,rule_text(system_db) ,log ,preview): return False
  if program("udevadm") and run_logged(log ,preview ,"udev" ,["udevadm" ,"control" ,"--reload"]):
    log.line("reloaded" ,"udev rules")
  return True

def configure(log ,preview):
  from System import open_system_database
  system_db = open_system_database(log)
  if system_db is None: return
  admin = fpa__admin()
  problem = path_changer(admin)
  if problem:
    log.line("warning" ,str(admin) ,f"the units run this program under root, but {problem}; until Account-Tree is"
             " installed where only root can change it, whoever can change it can act as root")
  if not DIRPA__UNIT.is_dir():
    log.error(str(DIRPA__UNIT) ,"no such directory; is this system run by systemd?"); return
  for name ,text in unit_text().items():
    file__write(DIRPA__UNIT / name ,text ,log ,preview)
  if run_logged(log ,preview ,"systemd" ,["systemctl" ,"daemon-reload"]):
    log.line("reloaded" ,"systemd units")
  if not rule__write(system_db ,log ,preview): return
  # tag the keys plugged in now, so that systemd has their device units
  if program("udevadm") and run_logged(log ,preview ,"udev" ,["udevadm" ,"trigger" ,"--action=change" ,"--subsystem-match=hidraw"]):
    log.line("reloaded" ,"the keys plugged in now" ,"udev ran its rules for them again")
