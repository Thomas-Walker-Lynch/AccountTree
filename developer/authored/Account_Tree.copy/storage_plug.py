# -*- mode: python; coding: utf-8; python-indent-offset: 2; indent-tabs-mode: nil -*-
"""
storage_plug - plugging and unplugging removable storage, opened by a key

A removable storage is a LUKS2 partition holding a file system whose
<storage root>/home/Account-Tree is a tree dir. Registering it records, in the
system database's storage_device table, its LUKS uuid, the storage database it
carries, its storage name and the directory it is mounted at.

  register(tree_dir ,name)      record a storage that is unlocked and mounted now
  unregister(name)              retire the record
  plug(device)                  unlock with a key, mount, attach, offer its
                                login accounts on the login screen, and watch
                                the key that unlocked it
  plug_all()                    plug every registered storage present and locked
  unplug(spec)                  end its accounts' processes, unmount their homes,
                                hide its accounts, unmount, lock, power off
  unplug_key(key)               unplug every storage that key unlocked

Which key unlocked a storage. The LUKS2 header holds a systemd-fido2 token:
the credential id of the key it was enrolled with, and a salt. A FIDO2 key
answers for a credential only if it made that credential, so plug offers the
header to each key present in turn, one at a time, naming the key's device
(fido2-device=/dev/hidrawN). systemd-cryptsetup first asks the key, without a
touch, whether it holds the credential; a key that does not is passed over at
once, and the key that does turns blue and waits for its button. The device
whose attempt opened the partition is the key. plug records it in
/run/Account-Tree/plugged/<storage name>.json and starts
AccountTree-key@hidrawN.service, which is bound to the key's device unit:
when the key is pulled out, systemd stops that service, and its ExecStop runs
unplug_key.

Runtime records are kept under /run, which is emptied at each boot, because a
storage unlocked before a reboot is locked after it.

Everything is done under a lock, /run/Account-Tree/lock, so that the plug
started by the drive and the plug started by the key, which arrive together
at boot, take turns.
"""

import os ,re ,json ,time ,fcntl ,signal ,shutil ,subprocess ,sqlite3
from pathlib import Path

import database
from database import Database ,DatabaseError
import kernel
import accounts_service
from accounts_service import AccountsServiceError
from common import SEPARATOR ,TREE_DIR_NAME ,act ,tree_dir_problem
from system import open_system_database
import same_account
import storage

RUN_DIR = Path("/run/Account-Tree")
PLUGGED_DIR = RUN_DIR / "plugged"
LOCK_PATH = RUN_DIR / "lock"
MAPPER_PREFIX = "AccountTree_"
# instanced by the kernel name of the key's device, such as hidraw3
KEY_UNIT_TEMPLATE = "AccountTree-key@{}.service"
MOUNT_OPTION = "nosuid,nodev"
# how long processes are given to end: after logind ends their sessions,
# after SIGTERM, and after SIGKILL
SESSION_WAIT_SECOND = 5
TERM_WAIT_SECOND = 10
KILL_WAIT_SECOND = 5

NAME_CHARACTER = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")


# --------------------------------------------------------------------------------
# running programs

def program(name ,*fallback):
  """
  The path of a program, from PATH or from the fallback paths, or None.
  """
  found = shutil.which(name)
  if found: return found
  for f in fallback:
    if os.access(f ,os.X_OK): return f
  return None

def systemd_cryptsetup_path():
  return program("systemd-cryptsetup" ,"/usr/lib/systemd/systemd-cryptsetup" ,"/lib/systemd/systemd-cryptsetup")

def run(argument ,input_text=None ,timeout=None):
  """
  Run a program; returns (exit status ,stdout ,stderr). A program that cannot
  be started gives status 127.
  """
  try:
    r = subprocess.run(argument ,input=input_text ,capture_output=True ,text=True ,timeout=timeout)
    return r.returncode ,r.stdout ,r.stderr
  except FileNotFoundError:
    return 127 ,"" ,f"{argument[0]}: not found"
  except subprocess.TimeoutExpired:
    return 124 ,"" ,f"{argument[0]}: no answer within {timeout} seconds"

def last_line(text):
  line_list = [l for l in text.strip().splitlines() if l.strip()]
  return line_list[-1] if line_list else ""

def run_logged(log ,preview ,subject ,argument ,input_text=None ,timeout=None):
  """
  Run a program that changes the system; in a preview, do nothing. Returns
  True when it succeeded, or would be run; logs its last line of error output
  and returns False when it failed.
  """
  if preview: return True
  status ,out ,err = run(argument ,input_text ,timeout)
  if status != 0:
    log.error(subject ,f"{os.path.basename(argument[0])} failed, status {status}: {last_line(err) or last_line(out)}")
    return False
  return True


# --------------------------------------------------------------------------------
# the lock

class Lock:
  depth = 0
  fd = None

  def __enter__(self):
    if Lock.depth == 0:
      RUN_DIR.mkdir(mode=0o700 ,exist_ok=True)
      Lock.fd = os.open(str(LOCK_PATH) ,os.O_RDWR | os.O_CREAT | os.O_CLOEXEC ,0o600)
      fcntl.flock(Lock.fd ,fcntl.LOCK_EX)
    Lock.depth += 1
    return self

  def __exit__(self ,*_):
    Lock.depth -= 1
    if Lock.depth == 0:
      os.close(Lock.fd)
      Lock.fd = None


# --------------------------------------------------------------------------------
# block devices, LUKS and mounts

def mount_source(path):
  """
  The source of the mount whose mount point is path, from
  /proc/self/mountinfo, or None. In each line the mount point is the fifth
  field, and the source follows the file system type after the field '-'.
  """
  target = os.path.abspath(str(path))
  for line in Path("/proc/self/mountinfo").read_text().splitlines():
    field = line.split(" ")
    if len(field) < 10 or "-" not in field: continue
    point = re.sub(r"\\([0-7]{3})" ,lambda m: chr(int(m.group(1) ,8)) ,field[4])
    if point == target:
      i = field.index("-")
      return field[i + 2]
  return None

def block_name(device):
  """
  The kernel's name of a block device, such as sdb1 or dm-0, from its path.
  """
  return os.path.basename(os.path.realpath(str(device)))

def dm_name(kernel_name):
  try:
    return Path(f"/sys/class/block/{kernel_name}/dm/name").read_text().strip()
  except OSError:
    return None

def dm_slave_list(kernel_name):
  try:
    return sorted(p.name for p in Path(f"/sys/class/block/{kernel_name}/slaves").iterdir())
  except OSError:
    return []

def dm_holder_list(kernel_name):
  """
  The device-mapper devices opened on a partition, such as the one LUKS makes.
  """
  try:
    return sorted(p.name for p in Path(f"/sys/class/block/{kernel_name}/holders").iterdir())
  except OSError:
    return []

def disk_of(kernel_name):
  """
  The whole disk a partition is on: sdb for sdb1. A whole disk is its own disk.
  """
  path = Path(f"/sys/class/block/{kernel_name}")
  try:
    real = path.resolve()
  except OSError:
    return None
  if (real / "partition").exists():
    return real.parent.name
  return kernel_name

def luks_uuid(device):
  status ,out ,_ = run(["cryptsetup" ,"luksUUID" ,str(device)])
  return out.strip() if status == 0 and out.strip() else None

def luks_fido2_token_list(device):
  """
  The systemd-fido2 tokens of a LUKS2 header, as dictionaries, from
  cryptsetup luksDump --dump-json-metadata.
  """
  status ,out ,err = run(["cryptsetup" ,"luksDump" ,"--dump-json-metadata" ,str(device)])
  if status != 0:
    raise ValueError(f"cannot read the LUKS2 header of {device}: {last_line(err)}")
  token = json.loads(out).get("tokens" ,{})
  return [t for t in token.values() if t.get("type") == "systemd-fido2"]

def key_list():
  """
  The FIDO2 keys plugged in: each hidraw device that udev marks
  ID_SECURITY_TOKEN=1, which systemd's 60-fido-id.rules sets for a device
  that speaks FIDO.
  """
  result = []
  for p in sorted(Path("/dev").glob("hidraw*")):
    status ,out ,_ = run(["udevadm" ,"info" ,"--query=property" ,f"--name={p}"])
    if status == 0 and "ID_SECURITY_TOKEN=1" in out.splitlines():
      result.append(str(p))
  return result

def is_device_unit_active(unit):
  status ,out ,_ = run(["systemctl" ,"show" ,"--property=ActiveState" ,"--value" ,unit])
  return status == 0 and out.strip() == "active"


# --------------------------------------------------------------------------------
# records

def registration_by_name(system_db ,name):
  return system_db.row("SELECT * FROM storage_device WHERE storage_name = ? AND state = 'registered'" ,(name,))

def registration_by_luks_uuid(system_db ,uuid_text):
  return system_db.row("SELECT * FROM storage_device WHERE luks_uuid = ? AND state = 'registered'" ,(uuid_text,))

def registration_list(system_db):
  return system_db.rows("SELECT * FROM storage_device WHERE state = 'registered' ORDER BY storage_name")

def plugged_path(name):
  return PLUGGED_DIR / f"{name}.json"

def plugged_read(name):
  try:
    return json.loads(plugged_path(name).read_text())
  except (OSError ,ValueError):
    return None

def plugged_write(record):
  PLUGGED_DIR.mkdir(mode=0o700 ,parents=True ,exist_ok=True)
  path = plugged_path(record["storage_name"])
  temporary = path.with_suffix(".tmp")
  temporary.write_text(json.dumps(record ,indent=2))
  os.replace(temporary ,path)

def plugged_list():
  try:
    return [r for r in (plugged_read(p.stem) for p in sorted(PLUGGED_DIR.glob("*.json"))) if r]
  except OSError:
    return []

def tree_dir_of(registration):
  return Path(registration["mount_dir"]) / "home" / TREE_DIR_NAME

def system_map_row_list(system_db ,database_uuid):
  return system_db.rows("SELECT * FROM system_map WHERE database_uuid = ? AND state = 'in use' ORDER BY system_account_name",
                        (database_uuid,))

def is_child_account(name):
  return SEPARATOR in name


# --------------------------------------------------------------------------------
# register and unregister

def register(tree_dir ,name ,log ,preview):
  """
  Record a removable storage. It is given by its tree dir, while it is
  unlocked and mounted, so that everything recorded is read from the storage
  itself: the LUKS2 partition under the mount, its LUKS uuid, the key enrolled
  in its header, and its database uuid.
  """
  problem = tree_dir_problem(tree_dir)
  if problem:
    log.error(str(tree_dir) ,problem); return
  tree_dir = Path(os.path.abspath(tree_dir))
  root = tree_dir.parent.parent
  if root == Path("/"):
    log.error(str(tree_dir) ,"is on the file system the system runs from, which is attached at boot and never unplugged"); return
  if not kernel.is_mount_point(root):
    log.error(str(root) ,"not a mount point; register a storage while it is unlocked and mounted"); return
  source = mount_source(root)
  mapped = block_name(source) if source else None
  if not mapped or dm_name(mapped) is None:
    log.error(str(root) ,f"mounted from {source}, which is not an unlocked LUKS2 partition"); return
  slave = dm_slave_list(mapped)
  if len(slave) != 1:
    log.error(str(root) ,f"{source} is made from {slave or 'nothing'}, not from one partition"); return
  partition = Path("/dev") / slave[0]
  uuid_text = luks_uuid(partition)
  if uuid_text is None:
    log.error(str(partition) ,"not a LUKS partition"); return
  log.line("found" ,str(partition) ,f"LUKS uuid {uuid_text}, unlocked as {dm_name(mapped)}, mounted at {root}")

  try:
    token = luks_fido2_token_list(partition)
  except ValueError as error:
    log.error(str(partition) ,str(error)); return
  if not token:
    log.error(str(partition) ,"no FIDO2 key is enrolled in its header. Enroll one, touching the key when it"
              f" blinks: systemd-cryptenroll --fido2-device=auto --fido2-with-client-pin=no {partition}"
              " then register again"); return
  for t in token:
    if t.get("fido2-clientPin-required"):
      log.line("warning" ,str(partition) ,f"the key in key slot {','.join(t.get('keyslots' ,[]))} was enrolled"
               " needing its FIDO2 PIN, which an automatic plug has no screen to ask for. Enroll it again with"
               " --fido2-with-client-pin=no; a key with its own PIN pad, such as the OnlyKey, keeps its PIN")
  log.line("found" ,f"{len(token)} FIDO2 key{'s' if len(token) != 1 else ''} enrolled" ,str(partition))

  if name is None: name = root.name
  if not name or not set(name) <= NAME_CHARACTER:
    log.error(name or "(empty)" ,"a storage name is letters, digits, '_', '.' and '-'; give one with the keyword name"); return

  system_db = open_system_database(log)
  if system_db is None: return
  storage_db = storage.open_storage_database(tree_dir ,log)
  if storage_db is None: return

  by_name = registration_by_name(system_db ,name)
  by_uuid = registration_by_luks_uuid(system_db ,uuid_text)
  if by_uuid is not None:
    if by_uuid["storage_name"] == name and by_uuid["mount_dir"] == str(root) and by_uuid["database_uuid"] == storage_db.uuid():
      log.line("kept" ,name ,"already registered")
    else:
      log.error(name ,f"LUKS uuid {uuid_text} is registered as {by_uuid['storage_name']}; unregister it first"); return
  elif by_name is not None:
    log.error(name ,f"the storage name is registered for LUKS uuid {by_name['luks_uuid']}; give another with the keyword name"); return
  else:
    with system_db.transaction(f"register storage {name}" ,preview) as t:
      t.execute("INSERT INTO storage_device(storage_name ,luks_uuid ,database_uuid ,mount_dir ,state ,registered)"
                " VALUES (? ,? ,? ,? ,'registered' ,?)" ,(name ,uuid_text ,storage_db.uuid() ,str(root) ,database.now()))
    log.line("recorded" ,f"storage {name}" ,f"LUKS uuid {uuid_text}, database {storage_db.short()}, mounted at {root}")

  if not system_map_row_list(system_db ,storage_db.uuid()):
    log.line("warning" ,name ,"none of its storage accounts is the same account as a system account here,"
             " so plugging it in mounts no home")

  import system_configuration
  system_configuration.rule_write(system_db ,log ,preview)

  # the storage is open now under another name, or under no Account-Tree record
  plugged = plugged_read(name)
  if plugged is None:
    plugged_record = {"storage_name": name ,"luks_uuid": uuid_text ,"partition": str(partition),
                      "mapper": dm_name(mapped) ,"mount_dir": str(root) ,"key": None ,"plugged": database.now()}
    if not preview: plugged_write(plugged_record)
    log.line("recorded" ,str(plugged_path(name)) ,"unlocked outside Account-Tree, so no key is watched;"
             f" unplug it this time with 'Account-Tree_admin storage unplug {name}'")

def unregister(name ,log ,preview):
  system_db = open_system_database(log)
  if system_db is None: return
  r = registration_by_name(system_db ,name)
  if r is None:
    log.error(name ,"no registered storage of that name"); return
  with system_db.transaction(f"unregister storage {name}" ,preview) as t:
    t.execute("UPDATE storage_device SET state = 'retired' ,retired = ? WHERE id = ?" ,(database.now() ,r["id"]))
  log.line("retired" ,f"storage {name}" ,f"LUKS uuid {r['luks_uuid']}")
  import system_configuration
  system_configuration.rule_write(system_db ,log ,preview)


# --------------------------------------------------------------------------------
# plug

def plug(device ,passphrase ,log ,preview):
  with Lock():
    return plug_locked(device ,passphrase ,log ,preview)

def plug_all(log ,preview):
  """
  Plug every registered storage whose partition is present and not unlocked.
  Run when a key is plugged in, for a drive that arrived before its key.
  """
  with Lock():
    system_db = open_system_database(log)
    if system_db is None: return
    found = False
    for r in registration_list(system_db):
      device = Path("/dev/disk/by-uuid") / r["luks_uuid"]
      if not device.exists(): continue
      found = True
      plug_locked(str(device) ,False ,log ,preview)
    if not found:
      log.line("skipped" ,"plug all" ,"no registered storage is plugged in")

def plug_locked(device ,passphrase ,log ,preview):
  system_db = open_system_database(log)
  if system_db is None: return
  if not os.path.exists(device):
    log.error(str(device) ,"no such device"); return
  uuid_text = luks_uuid(device)
  if uuid_text is None:
    log.line("skipped" ,str(device) ,"not a LUKS partition"); return
  r = registration_by_luks_uuid(system_db ,uuid_text)
  if r is None:
    log.line("skipped" ,str(device) ,f"LUKS uuid {uuid_text} is not a registered storage"); return
  name = r["storage_name"]
  partition = Path("/dev") / block_name(device)
  mount_dir = Path(r["mount_dir"])

  # unlock, unless it is unlocked already
  holder = [h for h in dm_holder_list(partition.name) if dm_name(h)]
  key_used = None
  if holder:
    mapper = dm_name(holder[0])
    previous = plugged_read(name)
    key_used = previous.get("key") if previous and previous.get("mapper") == mapper else None
    log.line("kept" ,f"storage {name}" ,f"already unlocked as {mapper}")
  else:
    mapper = MAPPER_PREFIX + name
    if passphrase:
      if preview:
        log.line("unlocked" ,f"storage {name}" ,"would ask for the passphrase")
      else:
        # cryptsetup asks on the terminal it was run from
        status = subprocess.run(["cryptsetup" ,"open" ,str(partition) ,mapper]).returncode
        if status != 0:
          log.error(f"storage {name}" ,f"cryptsetup open failed, status {status}"); return
        log.line("unlocked" ,f"storage {name}" ,f"as {mapper}, by passphrase; no key is watched")
    else:
      key_used = key_unlock(partition ,mapper ,name ,log ,preview)
      if key_used is None: return

  # mount
  mapped_device = Path("/dev/mapper") / mapper
  if kernel.is_mount_point(mount_dir):
    log.line("kept" ,str(mount_dir) ,"already mounted")
  else:
    if not mount_dir.exists():
      if not act(log ,preview ,str(mount_dir) ,os.makedirs ,mount_dir ,0o700): return
      if not act(log ,preview ,str(mount_dir) ,os.chmod ,mount_dir ,0o700): return
      log.line("made" ,str(mount_dir) ,"root only, mode 0700")
    if not run_logged(log ,preview ,str(mount_dir) ,["mount" ,"-o" ,MOUNT_OPTION ,str(mapped_device) ,str(mount_dir)]): return
    log.line("mounted" ,str(mount_dir) ,f"{mapped_device}, {MOUNT_OPTION}")

  record = {"storage_name": name ,"luks_uuid": uuid_text ,"partition": str(partition) ,"mapper": mapper,
            "mount_dir": str(mount_dir) ,"key": key_used ,"plugged": database.now()}
  if not preview: plugged_write(record)

  # attach, and offer its login accounts on the login screen
  tree_dir = tree_dir_of(r)
  error_before = log.error_count
  storage.attach(str(tree_dir) ,False ,log ,preview)
  if log.error_count == error_before:
    is_hidden_set_all(system_db ,r["database_uuid"] ,False ,log ,preview)

  if key_used:
    key_watch(key_used ,name ,log ,preview)

def key_unlock(partition ,mapper ,name ,log ,preview):
  """
  Offer the partition to each key plugged in, one at a time. Returns the
  device of the key that opened it, or None.
  """
  tool = systemd_cryptsetup_path()
  if tool is None:
    log.error(f"storage {name}" ,"systemd-cryptsetup is not installed"); return None
  key = key_list()
  if not key:
    log.line("waiting" ,f"storage {name}" ,"no key is plugged in; plugging one in unlocks it"); return None
  if preview:
    log.line("unlocked" ,f"storage {name}" ,f"would offer it to {', '.join(key)}, one at a time")
    return key[0]
  for k in key:
    log.line("offered" ,f"storage {name}" ,f"to the key at {k}; touch it when it lights")
    # headless: never fall back to asking for a passphrase, which no one is
    # shown here; the administrator unlocks by passphrase with the keyword
    status ,out ,err = run([tool ,"attach" ,mapper ,str(partition) ,"none" ,f"fido2-device={k},headless=yes"] ,timeout=120)
    if status == 0:
      log.line("unlocked" ,f"storage {name}" ,f"as {mapper}, by the key at {k}")
      return k
    log.line("refused" ,f"the key at {k}" ,last_line(err) or last_line(out) or f"status {status}")
  log.error(f"storage {name}" ,"no key plugged in opened it")
  return None

def key_watch(key ,name ,log ,preview):
  """
  Start the service bound to the key's device unit, so that pulling the key
  out unplugs what it unlocked.
  """
  kernel_name = os.path.basename(key)
  device_unit = f"dev-{kernel_name}.device"
  unit = KEY_UNIT_TEMPLATE.format(kernel_name)
  if not preview and not is_device_unit_active(device_unit):
    log.line("warning" ,f"storage {name}" ,f"systemd has no {device_unit}, so pulling the key out will not unplug it;"
             " run 'Account-Tree_admin system configure', which tags keys for systemd")
    return
  if not run_logged(log ,preview ,unit ,["systemctl" ,"start" ,"--no-block" ,unit]): return
  log.line("watching" ,key ,f"{unit}; pulling the key out unplugs {name}")

def is_hidden_set_all(system_db ,database_uuid ,hidden ,log ,preview):
  """
  Offer or hide the storage's login accounts on the login screen. Child
  accounts are entered from their parent, never through the login screen, and
  are always hidden.
  """
  changed = False
  for m in system_map_row_list(system_db ,database_uuid):
    name = m["system_account_name"]
    value = hidden or is_child_account(name)
    if preview:
      log.line("set" ,name ,f"SystemAccount={'true' if value else 'false'} for the login screen"); continue
    try:
      is_changed = accounts_service.is_hidden_set(name ,value)
    except AccountsServiceError as error:
      log.error(name ,str(error)); continue
    if is_changed is None:
      log.line("skipped" ,name ,"this system has no AccountsService"); return
    how = ("hidden from" if value else "offered on") + " the login screen"
    if is_changed:
      log.line("set" ,name ,how); changed = True
    else:
      log.line("kept" ,name ,how)
  if changed:
    # accounts-daemon reads an account's file when it loads the account
    run(["systemctl" ,"try-restart" ,"accounts-daemon.service"])


# --------------------------------------------------------------------------------
# unplug

def unplug(spec ,log ,preview):
  with Lock():
    return unplug_locked(spec ,log ,preview)

def unplug_key(key ,log ,preview):
  """
  Unplug every storage the key unlocked. Run by the key's service when the key
  is pulled out.
  """
  if not key.startswith("/"): key = "/" + key
  with Lock():
    found = False
    for record in plugged_list():
      if record.get("key") == key:
        found = True
        unplug_locked(record["storage_name"] ,log ,preview)
    if not found:
      log.line("skipped" ,key ,"it unlocked no storage plugged in now")

def registration_of(system_db ,spec):
  """
  The registration a spec names: a storage name, or a device path. A
  partition named by /dev/disk/by-uuid/<LUKS uuid>, or one a runtime record
  holds, is found even after the drive is gone, when the device can no longer
  be read.
  """
  r = registration_by_name(system_db ,spec)
  if r is not None: return r
  path = Path(spec)
  # the drive's service names its partition, /dev/sdb1, which is gone by the
  # time a drive pulled out is unplugged; the runtime record remembers it
  for record in plugged_list():
    if record.get("partition") == str(path):
      return registration_by_name(system_db ,record["storage_name"])
  if path.parent == Path("/dev/disk/by-uuid"):
    return registration_by_luks_uuid(system_db ,path.name)
  if path.exists():
    uuid_text = luks_uuid(path)
    if uuid_text: return registration_by_luks_uuid(system_db ,uuid_text)
  return None

def unplug_locked(spec ,log ,preview):
  system_db = open_system_database(log)
  if system_db is None: return
  r = registration_of(system_db ,spec)
  if r is None:
    # the drive's own service stops after the key's unplug has powered it off;
    # by then its record is gone and its partition cannot be read
    log.line("skipped" ,str(spec) ,"nothing Account-Tree plugged in is on it"); return
  name = r["storage_name"]
  mount_dir = Path(r["mount_dir"])
  record = plugged_read(name)
  mapper = record["mapper"] if record else MAPPER_PREFIX + name
  row_list = system_map_row_list(system_db ,r["database_uuid"])
  home_mounted = [m for m in row_list if kernel.is_mount_point(Path("/home") / m["system_account_name"])]
  is_open = Path(f"/dev/mapper/{mapper}").exists()
  if not (record or home_mounted or is_open or kernel.is_mount_point(mount_dir)):
    log.line("skipped" ,f"storage {name}" ,"not plugged in"); return

  # 1. end every process of its accounts, whoever is at the console
  process_end([m["system_uid"] for m in row_list] ,log ,preview)

  # 2. unmount their homes, and record the detach while the storage can be read
  for m in home_mounted:
    target = Path("/home") / m["system_account_name"]
    mount_remove(target ,log ,preview)
  detach_record(system_db ,r ,log ,preview)

  # 3. hide its accounts from the login screen
  is_hidden_set_all(system_db ,r["database_uuid"] ,True ,log ,preview)

  # 4. unmount the storage and lock it
  if kernel.is_mount_point(mount_dir):
    if not preview: os.sync()
    mount_remove(mount_dir ,log ,preview)
  if is_open:
    if run_logged(log ,preview ,mapper ,["cryptsetup" ,"close" ,mapper]):
      log.line("locked" ,f"storage {name}" ,f"{mapper} closed")
    elif run_logged(log ,preview ,mapper ,["cryptsetup" ,"close" ,"--deferred" ,mapper]):
      log.line("locked" ,f"storage {name}" ,f"{mapper} closes when the last file open on it is closed")

  # 5. power the drive off, so that its light going out says it is safe to pull
  partition = Path(record["partition"]) if record else Path("/dev/disk/by-uuid") / r["luks_uuid"]
  if partition.exists():
    disk = disk_of(block_name(partition))
    tool = program("udisksctl")
    if disk is None:
      pass
    elif tool is None:
      log.line("skipped" ,f"/dev/{disk}" ,"udisksctl is not installed, so the drive is locked but still powered")
    elif run_logged(log ,preview ,f"/dev/{disk}" ,[tool ,"power-off" ,"-b" ,f"/dev/{disk}" ,"--no-user-interaction"]):
      log.line("powered off" ,f"/dev/{disk}" ,"safe to pull out")
  if not preview:
    try:
      plugged_path(name).unlink()
    except FileNotFoundError:
      pass

def process_uid_set(pid_path):
  """
  The real, effective, saved and file system uids of a process.
  """
  try:
    for line in (pid_path / "status").read_text().splitlines():
      if line.startswith("Uid:"):
        return {int(x) for x in line.split()[1:]}
  except (OSError ,ValueError):
    pass
  return set()

def is_zombie(pid_path):
  """
  Whether a process has ended and waits only for its parent to collect its
  exit status. It holds no file and no mount, so it is not waited for.
  """
  try:
    stat_text = (pid_path / "stat").read_text()
  except OSError:
    return True
  # the command name is in parentheses and can hold spaces; the state follows it
  return stat_text[stat_text.rindex(")") + 2:].startswith("Z")

def process_list(uid_set):
  result = []
  for p in Path("/proc").iterdir():
    if p.name.isdigit() and process_uid_set(p) & uid_set and not is_zombie(p):
      result.append(int(p.name))
  return result

def process_wait(uid_set ,second):
  deadline = time.monotonic() + second
  while time.monotonic() < deadline:
    if not process_list(uid_set): return []
    time.sleep(0.2)
  return process_list(uid_set)

def process_end(uid_list ,log ,preview):
  """
  End every process running under the uids: logind ends their sessions; what
  remains is sent SIGTERM, and what remains after that, SIGKILL.
  """
  uid_set = set(uid_list)
  if not uid_set: return
  pid = process_list(uid_set)
  if not pid:
    log.line("kept" ,"processes" ,"none runs under its accounts"); return
  if preview:
    log.line("ended" ,f"{len(pid)} processes" ,"of its accounts"); return
  for u in sorted(uid_set):
    run(["loginctl" ,"terminate-user" ,str(u)])
  remaining = process_wait(uid_set ,SESSION_WAIT_SECOND)
  for sig ,wait in ((signal.SIGTERM ,TERM_WAIT_SECOND) ,(signal.SIGKILL ,KILL_WAIT_SECOND)):
    if not remaining: break
    for p in remaining:
      try:
        os.kill(p ,sig)
      except ProcessLookupError:
        pass
    remaining = process_wait(uid_set ,wait)
  if remaining:
    log.error("processes" ,f"{len(remaining)} still run under its accounts: {', '.join(map(str ,remaining[:8]))}")
  else:
    log.line("ended" ,f"{len(pid)} processes" ,"of its accounts")

def mount_remove(target ,log ,preview):
  """
  Unmount target; when something still holds it open, detach it from the
  tree so that no one new can reach it, and the kernel finishes when the last
  holder lets go.
  """
  if preview:
    log.line("unmounted" ,str(target)); return True
  if kernel.LIBC.umount2(os.fsencode(str(target)) ,0) == 0:
    log.line("unmounted" ,str(target)); return True
  if kernel.LIBC.umount2(os.fsencode(str(target)) ,kernel.MNT_DETACH) == 0:
    log.line("unmounted" ,str(target) ,"something held it open, so it was detached, and closes when that lets go")
    return True
  log.error(str(target) ,f"umount failed: {os.strerror(kernel.ctypes.get_errno())}")
  return False

def detach_record(system_db ,r ,log ,preview):
  """
  Record the detach in both databases while the storage can still be read.
  When the drive has been pulled out first, the storage database cannot be
  written; the next attach finds the head it last recorded.
  """
  tree_dir = tree_dir_of(r)
  storage_db = None
  try:
    storage_db = Database.open(database.storage_database_path(tree_dir) ,"storage")
    same_account.system_seen_record(storage_db ,system_db.uuid() ,log ,preview)
    series = same_account.head_seen_record(system_db ,storage_db ,"detached" ,None ,preview)
    log.line("recorded" ,f"database {storage_db.short()} detached" ,f"head at series {series} remembered here")
  except (OSError ,sqlite3.Error ,DatabaseError) as error:
    log.line("skipped" ,f"storage {r['storage_name']}" ,f"the detach was not recorded, the storage cannot be read: {error}")
  finally:
    # an open database file would hold the storage's file system busy
    if storage_db is not None: storage_db.close()
