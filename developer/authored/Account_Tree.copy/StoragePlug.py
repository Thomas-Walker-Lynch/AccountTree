# -*- mode: python; coding: utf-8; python-indent-offset: 2; indent-tabs-mode: nil -*-
"""
StoragePlug - plugging and unplugging removable storage, opened by a key

A removable storage is a LUKS2 partition holding a file system with
Account-Tree's directory, home/Account-Tree. Registering it records, in the
system database's storage_device table, its LUKS uuid, the storage database it
carries, its storage name and the directory it is mounted at.

  register(dirp__storage ,name)  record a storage that is unlocked and mounted now
  unregister(name)              retire the record
  plug(device)                  unlock with a key, mount, attach, offer its
                                login accounts on the login screen, and watch
                                the key that unlocked it
  plug_all()                    plug every registered storage present and locked
  unplug(spec)                  end its accounts' processes, unmount their homes,
                                delete the system accounts Account-Tree made for
                                them, hide the adopted ones, unmount, lock,
                                power off
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
Account-Tree_key@hidrawN.service, which is bound to the key's device unit:
when the key is pulled out, systemd stops that service, and its ExecStop runs
unplug_key.

A key that resets looks the same as one pulled out. The OnlyKey drops off the
bus when it locks itself after a time without use, and rejoins under a new
USB device number; its hidraw devices come back about eight seconds later.
So unplug_key first freezes the storage's file system: the kernel writes out
everything pending and holds every new write, so that the drive is
consistent, and pulling it is safe, from that moment. It then waits up to
KEY_RETURN__SECOND for a key of the same identity, its USB port, vendor,
product and serial, to come back. When one does, the file system is thawed
and the programs carry on, having paused; Account-Tree_key-arrive.service,
started by the key's return, then watches the key's new device. When none
does, or the drive itself goes, the file system is thawed so that its
programs can be ended, and the unplug goes on as before.

A key's identity is not proof that it is the same key: every OnlyKey reports
the serial 1000000000. It decides only whether a disappearance was a reset,
on the same port, within the wait.

Runtime records are kept under /run, which is emptied at each boot, because a
storage unlocked before a reboot is locked after it.

Everything is done under a lock, /run/Account-Tree/lock, so that the plug
started by the drive and the plug started by the key, which arrive together
at boot, take turns.
"""

import os ,re ,json ,time ,errno ,fcntl ,signal ,shutil ,subprocess ,sqlite3
from pathlib import Path

import Database
from Database import DatabaseError
import Kernel
import AccountsService
from AccountsService import AccountsServiceError
from Common import SEPARATOR ,act ,dirp__storage__problem ,dirpa__Account_Tree
from System import open_system_database
import SameAccount
import Storage

DIRPA__RUN = Path("/run/Account-Tree")
DIRPA__PLUGGED = DIRPA__RUN / "plugged"
FPA__LOCK = DIRPA__RUN / "lock"
MAPPER_PREFIX = "Account-Tree_"
# instanced by the kernel name of the key's device, such as hidraw3
KEY_UNIT__TEMPLATE = "Account-Tree_key@{}.service"
MOUNT_OPTION = "nosuid,nodev"
# how long processes are given to end: after logind ends their sessions,
# after SIGTERM, and after SIGKILL
SESSION_WAIT__SECOND = 5
TERM_WAIT__SECOND = 10
KILL_WAIT__SECOND = 5
# how long a key that dropped off the bus is waited for, while its storage is frozen
KEY_RETURN__SECOND = 15
# the ioctl requests of linux/fs.h that freeze and thaw a file system
FIFREEZE = 0xC0045877
FITHAW = 0xC0045878

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

def fpa__systemd_cryptsetup():
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
  list__line = [l for l in text.strip().splitlines() if l.strip()]
  return list__line[-1] if list__line else ""

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
      DIRPA__RUN.mkdir(mode=0o700 ,exist_ok=True)
      Lock.fd = os.open(str(FPA__LOCK) ,os.O_RDWR | os.O_CREAT | os.O_CLOEXEC ,0o600)
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

def mount_source(dirp__mount_point):
  """
  The source of the mount whose mount point is path, from
  /proc/self/mountinfo, or None. In each line the mount point is the fifth
  field, and the source follows the file system type after the field '-'.
  """
  dirpa__mount_point = os.path.abspath(str(dirp__mount_point))
  for line in Path("/proc/self/mountinfo").read_text().splitlines():
    field = line.split(" ")
    if len(field) < 10 or "-" not in field: continue
    point = re.sub(r"\\([0-7]{3})" ,lambda m: chr(int(m.group(1) ,8)) ,field[4])
    if point == dirpa__mount_point:
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

def list__dm_slave(kernel_name):
  try:
    return sorted(p.name for p in Path(f"/sys/class/block/{kernel_name}/slaves").iterdir())
  except OSError:
    return []

def list__dm_holder(kernel_name):
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
  dirpa__sys_block = Path(f"/sys/class/block/{kernel_name}")
  try:
    real = dirpa__sys_block.resolve()
  except OSError:
    return None
  if (real / "partition").exists():
    return real.parent.name
  return kernel_name

def luks_uuid(device):
  status ,out ,_ = run(["cryptsetup" ,"luksUUID" ,str(device)])
  return out.strip() if status == 0 and out.strip() else None

def list__luks_fido2_token(device):
  """
  The systemd-fido2 tokens of a LUKS2 header, as dictionaries, from
  cryptsetup luksDump --dump-json-metadata.
  """
  status ,out ,err = run(["cryptsetup" ,"luksDump" ,"--dump-json-metadata" ,str(device)])
  if status != 0:
    raise ValueError(f"cannot read the LUKS2 header of {device}: {last_line(err)}")
  token = json.loads(out).get("tokens" ,{})
  return [t for t in token.values() if t.get("type") == "systemd-fido2"]

def list__key():
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

def key__identity(key):
  """
  The identity of the USB device a hidraw device belongs to: its port, such
  as 1-4, and its vendor, product and serial, read from sysfs. None when the
  device is gone or is not on USB.
  """
  try:
    dirpa__device = Path(f"/sys/class/hidraw/{os.path.basename(key)}/device").resolve()
  except OSError:
    return None
  for dirpa__each in [dirpa__device] + list(dirpa__device.parents):
    if (dirpa__each / "idVendor").exists():
      def read(name):
        try:
          return (dirpa__each / name).read_text().strip()
        except OSError:
          return ""
      return f"{dirpa__each.name}:{read('idVendor')}:{read('idProduct')}:{read('serial')}"
  return None

def key__by_identity(identity):
  """The device of a key plugged in now with that identity, or None."""
  for k in list__key():
    if key__identity(k) == identity:
      return k
  return None

def file_system__freeze(dirp__mount ,log ,preview):
  """
  Freeze the file system mounted at dirp__mount: the kernel writes out what is
  pending and holds every new write until it is thawed. Returns True when it
  is frozen now.
  """
  if preview:
    log.line("frozen" ,str(dirp__mount)); return True
  try:
    fd = os.open(str(dirp__mount) ,os.O_RDONLY | os.O_DIRECTORY)
    try:
      fcntl.ioctl(fd ,FIFREEZE ,0)
    finally:
      os.close(fd)
  except OSError as error:
    log.line("skipped" ,str(dirp__mount) ,f"not frozen: {error.strerror}")
    return False
  log.line("frozen" ,str(dirp__mount) ,"its writes are held; the drive is consistent")
  return True

def file_system__thaw(dirp__mount ,log ,preview):
  """Thaw a frozen file system. One that is not frozen is left as it is."""
  if preview:
    log.line("thawed" ,str(dirp__mount)); return
  try:
    fd = os.open(str(dirp__mount) ,os.O_RDONLY | os.O_DIRECTORY)
    try:
      fcntl.ioctl(fd ,FITHAW ,0)
    finally:
      os.close(fd)
  except OSError as error:
    if error.errno != errno.EINVAL:  # EINVAL: it was not frozen
      log.line("skipped" ,str(dirp__mount) ,f"not thawed: {error.strerror}")
    return
  log.line("thawed" ,str(dirp__mount) ,"its held writes go ahead")

def unit__state(unit):
  status ,out ,_ = run(["systemctl" ,"show" ,"--property=ActiveState" ,"--value" ,unit])
  return out.strip() if status == 0 else ""

def is__device_unit_active(unit):
  status ,out ,_ = run(["systemctl" ,"show" ,"--property=ActiveState" ,"--value" ,unit])
  return status == 0 and out.strip() == "active"


# --------------------------------------------------------------------------------
# records

def registration__by_name(system_db ,name):
  return system_db.row("SELECT * FROM storage_device WHERE storage_name = ? AND state = 'registered'" ,(name,))

def registration__by_luks_uuid(system_db ,uuid_text):
  return system_db.row("SELECT * FROM storage_device WHERE luks_uuid = ? AND state = 'registered'" ,(uuid_text,))

def list__registration(system_db):
  return system_db.rows("SELECT * FROM storage_device WHERE state = 'registered' ORDER BY storage_name")

def fpa__plugged(name):
  return DIRPA__PLUGGED / f"{name}.json"

def plugged__read(name):
  try:
    return json.loads(fpa__plugged(name).read_text())
  except (OSError ,ValueError):
    return None

def plugged__write(record):
  DIRPA__PLUGGED.mkdir(mode=0o700 ,parents=True ,exist_ok=True)
  fpa__record = fpa__plugged(record["storage_name"])
  temporary = fpa__record.with_suffix(".tmp")
  temporary.write_text(json.dumps(record ,indent=2))
  os.replace(temporary ,fpa__record)

def list__plugged():
  try:
    return [r for r in (plugged__read(p.stem) for p in sorted(DIRPA__PLUGGED.glob("*.json"))) if r]
  except OSError:
    return []

def list__system_map_row(system_db ,database_uuid):
  return system_db.rows("SELECT * FROM system_map WHERE database_uuid = ? AND state = 'in use' ORDER BY system_account__name",
                        (database_uuid,))

def is__child_account(name):
  return SEPARATOR in name


# --------------------------------------------------------------------------------
# register and unregister

def register(dirp__storage ,name ,log ,preview):
  """
  Record a removable storage. It is given by its root, while it is
  unlocked and mounted, so that everything recorded is read from the storage
  itself: the LUKS2 partition under the mount, its LUKS uuid, the key enrolled
  in its header, and its database uuid.
  """
  problem = dirp__storage__problem(dirp__storage)
  if problem:
    log.error(str(dirp__storage) ,problem); return
  dirpa__storage_root = Path(os.path.abspath(dirp__storage))
  if dirpa__storage_root == Path("/"):
    log.error(str(dirpa__storage_root) ,"is the root of the file system the system runs from, which is attached at boot and never unplugged"); return
  if not Kernel.is__mount_point(dirpa__storage_root):
    log.error(str(dirpa__storage_root) ,"not a mount point; register a storage while it is unlocked and mounted"); return
  source = mount_source(dirpa__storage_root)
  mapped = block_name(source) if source else None
  if not mapped or dm_name(mapped) is None:
    log.error(str(dirpa__storage_root) ,f"mounted from {source}, which is not an unlocked LUKS2 partition"); return
  slave = list__dm_slave(mapped)
  if len(slave) != 1:
    log.error(str(dirpa__storage_root) ,f"{source} is made from {slave or 'nothing'}, not from one partition"); return
  partition = Path("/dev") / slave[0]
  uuid_text = luks_uuid(partition)
  if uuid_text is None:
    log.error(str(partition) ,"not a LUKS partition"); return
  log.line("found" ,str(partition) ,f"LUKS uuid {uuid_text}, unlocked as {dm_name(mapped)}, mounted at {dirpa__storage_root}")

  try:
    token = list__luks_fido2_token(partition)
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

  if name is None: name = dirpa__storage_root.name
  if not name or not set(name) <= NAME_CHARACTER:
    log.error(name or "(empty)" ,"a storage name is letters, digits, '_', '.' and '-'; give one with the keyword name"); return

  system_db = open_system_database(log)
  if system_db is None: return
  storage_db = Storage.open_storage_database(dirpa__storage_root ,log)
  if storage_db is None: return

  by_name = registration__by_name(system_db ,name)
  by_uuid = registration__by_luks_uuid(system_db ,uuid_text)
  if by_uuid is not None:
    if by_uuid["storage_name"] == name and by_uuid["dirpa__mount"] == str(dirpa__storage_root) and by_uuid["database_uuid"] == storage_db.uuid():
      log.line("kept" ,name ,"already registered")
    else:
      log.error(name ,f"LUKS uuid {uuid_text} is registered as {by_uuid['storage_name']}; unregister it first"); return
  elif by_name is not None:
    log.error(name ,f"the storage name is registered for LUKS uuid {by_name['luks_uuid']}; give another with the keyword name"); return
  else:
    with system_db.transaction(f"register storage {name}" ,preview) as t:
      t.execute("INSERT INTO storage_device(storage_name ,luks_uuid ,database_uuid ,dirpa__mount ,state ,registered)"
                " VALUES (? ,? ,? ,? ,'registered' ,?)" ,(name ,uuid_text ,storage_db.uuid() ,str(dirpa__storage_root) ,Database.now()))
    log.line("recorded" ,f"storage {name}" ,f"LUKS uuid {uuid_text}, database {storage_db.short()}, mounted at {dirpa__storage_root}")

  if not list__system_map_row(system_db ,storage_db.uuid()):
    log.line("warning" ,name ,"none of its storage accounts is the same account as a system account here,"
             " so plugging it in mounts no home")

  import SystemConfiguration
  SystemConfiguration.rule__write(system_db ,log ,preview)

  # the storage is open now under another name, or under no Account-Tree record
  plugged = plugged__read(name)
  if plugged is None:
    plugged_record = {"storage_name": name ,"luks_uuid": uuid_text ,"partition": str(partition),
                      "mapper": dm_name(mapped) ,"dirpa__mount": str(dirpa__storage_root) ,"key": None ,"plugged": Database.now()}
    if not preview: plugged__write(plugged_record)
    log.line("recorded" ,str(fpa__plugged(name)) ,"unlocked outside Account-Tree, so no key is watched;"
             f" unplug it this time with 'Account-Tree_admin storage unplug {name}'")

def unregister(name ,log ,preview):
  system_db = open_system_database(log)
  if system_db is None: return
  r = registration__by_name(system_db ,name)
  if r is None:
    log.error(name ,"no registered storage of that name"); return
  with system_db.transaction(f"unregister storage {name}" ,preview) as t:
    t.execute("UPDATE storage_device SET state = 'retired' ,retired = ? WHERE id = ?" ,(Database.now() ,r["id"]))
  log.line("retired" ,f"storage {name}" ,f"LUKS uuid {r['luks_uuid']}")
  import SystemConfiguration
  SystemConfiguration.rule__write(system_db ,log ,preview)


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
    for r in list__registration(system_db):
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
  r = registration__by_luks_uuid(system_db ,uuid_text)
  if r is None:
    log.line("skipped" ,str(device) ,f"LUKS uuid {uuid_text} is not a registered storage"); return
  name = r["storage_name"]
  partition = Path("/dev") / block_name(device)
  dirpa__mount = Path(r["dirpa__mount"])

  # unlock, unless it is unlocked already
  holder = [h for h in list__dm_holder(partition.name) if dm_name(h)]
  key_used = None
  previous = None
  if holder:
    mapper = dm_name(holder[0])
    previous = plugged__read(name)
    if previous and previous.get("mapper") == mapper:
      key_used = previous.get("key")
      # a key that reset has come back under another device
      if previous.get("key_identity"):
        key_used = key__by_identity(previous["key_identity"]) or key_used
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
      key_used = key__unlock(partition ,mapper ,name ,log ,preview)
      if key_used is None: return

  # mount
  mapped_device = Path("/dev/mapper") / mapper
  if Kernel.is__mount_point(dirpa__mount):
    log.line("kept" ,str(dirpa__mount) ,"already mounted")
    # left frozen by an unplug that did not finish
    file_system__thaw(dirpa__mount ,log ,preview)
  else:
    if not dirpa__mount.exists():
      if not act(log ,preview ,str(dirpa__mount) ,os.makedirs ,dirpa__mount ,0o700): return
      if not act(log ,preview ,str(dirpa__mount) ,os.chmod ,dirpa__mount ,0o700): return
      log.line("made" ,str(dirpa__mount) ,"root only, mode 0700")
    if not run_logged(log ,preview ,str(dirpa__mount) ,["mount" ,"-o" ,MOUNT_OPTION ,str(mapped_device) ,str(dirpa__mount)]): return
    log.line("mounted" ,str(dirpa__mount) ,f"{mapped_device}, {MOUNT_OPTION}")

  record = {"storage_name": name ,"luks_uuid": uuid_text ,"partition": str(partition) ,"mapper": mapper,
            "dirpa__mount": str(dirpa__mount) ,"key": key_used ,"plugged": Database.now()}
  identity = key__identity(key_used) if key_used else None
  if identity is None and key_used and holder and previous:
    identity = previous.get("key_identity")
  record["key_identity"] = identity
  if not preview: plugged__write(record)

  # attach, and offer its login accounts on the login screen
  error_before = log.count_of__error
  Storage.attach(r["dirpa__mount"] ,False ,log ,preview)
  if log.count_of__error == error_before:
    is__hidden__set_all(system_db ,r["database_uuid"] ,False ,log ,preview)

  if key_used:
    key__watch(key_used ,name ,log ,preview)

def key__unlock(partition ,mapper ,name ,log ,preview):
  """
  Offer the partition to each key plugged in, one at a time. Returns the
  device of the key that opened it, or None.
  """
  tool = fpa__systemd_cryptsetup()
  if tool is None:
    log.error(f"storage {name}" ,"systemd-cryptsetup is not installed"); return None
  key = list__key()
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

def key__watch(key ,name ,log ,preview):
  """
  Start the service bound to the key's device unit, so that pulling the key
  out unplugs what it unlocked.
  """
  kernel_name = os.path.basename(key)
  device_unit = f"dev-{kernel_name}.device"
  unit = KEY_UNIT__TEMPLATE.format(kernel_name)
  if not preview:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and unit__state(unit) in ("deactivating" ,"activating"):
      time.sleep(0.2)
  if not preview and not is__device_unit_active(device_unit):
    log.line("warning" ,f"storage {name}" ,f"systemd has no {device_unit}, so pulling the key out will not unplug it;"
             " run 'Account-Tree_admin system configure', which tags keys for systemd")
    return
  if not run_logged(log ,preview ,unit ,["systemctl" ,"start" ,"--no-block" ,unit]): return
  log.line("watching" ,key ,f"{unit}; pulling the key out unplugs {name}")

def is__hidden__set_all(system_db ,database_uuid ,hidden ,log ,preview):
  """
  Offer or hide the storage's login accounts on the login screen. Child
  accounts are entered from their parent, never through the login screen, and
  are always hidden.
  """
  changed = False
  for m in list__system_map_row(system_db ,database_uuid):
    name = m["system_account__name"]
    value = hidden or is__child_account(name)
    if preview:
      log.line("set" ,name ,f"SystemAccount={'true' if value else 'false'} for the login screen"); continue
    try:
      is__changed = AccountsService.is__hidden__set(name ,value)
    except AccountsServiceError as error:
      log.error(name ,str(error)); continue
    if is__changed is None:
      log.line("skipped" ,name ,"this system has no AccountsService"); return
    how = ("hidden from" if value else "offered on") + " the login screen"
    if is__changed:
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
    list__record = [r for r in list__plugged() if r.get("key") == key]
    if not list__record:
      log.line("skipped" ,key ,"it unlocked no storage plugged in now"); return
    list__frozen = [r for r in list__record if file_system__freeze(r["dirpa__mount"] ,log ,preview)]
    returned = None
    try:
      identity = list__record[0].get("key_identity")
      if identity and not preview:
        log.line("waiting" ,key ,f"up to {KEY_RETURN__SECOND} seconds for the key to come back")
        deadline = time.monotonic() + KEY_RETURN__SECOND
        while time.monotonic() < deadline:
          returned = key__by_identity(identity)
          if returned: break
          # a drive pulled out while waiting is not waited for
          if not all(Path(r["partition"]).exists() for r in list__record): break
          time.sleep(0.5)
    finally:
      for r in list__frozen:
        file_system__thaw(r["dirpa__mount"] ,log ,preview)
    if returned:
      for r in list__record:
        r["key"] = returned
        plugged__write(r)
        log.line("kept" ,f"storage {r['storage_name']}" ,f"the key came back as {returned}; a reset, not a removal")
      # the key's return started Account-Tree_key-arrive.service, which waits
      # for this lock and then watches the key's new device
      return
    for r in list__record:
      unplug_locked(r["storage_name"] ,log ,preview)

def registration_of(system_db ,spec):
  """
  The registration a spec names: a storage name, or a device path. A
  partition named by /dev/disk/by-uuid/<LUKS uuid>, or one a runtime record
  holds, is found even after the drive is gone, when the device can no longer
  be read.
  """
  r = registration__by_name(system_db ,spec)
  if r is not None: return r
  fsnodp__spec = Path(spec)
  # the drive's service names its partition, /dev/sdb1, which is gone by the
  # time a drive pulled out is unplugged; the runtime record remembers it
  for record in list__plugged():
    if record.get("partition") == str(fsnodp__spec):
      return registration__by_name(system_db ,record["storage_name"])
  if fsnodp__spec.parent == Path("/dev/disk/by-uuid"):
    return registration__by_luks_uuid(system_db ,fsnodp__spec.name)
  if fsnodp__spec.exists():
    uuid_text = luks_uuid(fsnodp__spec)
    if uuid_text: return registration__by_luks_uuid(system_db ,uuid_text)
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
  dirpa__mount = Path(r["dirpa__mount"])
  record = plugged__read(name)
  mapper = record["mapper"] if record else MAPPER_PREFIX + name
  list__row = list__system_map_row(system_db ,r["database_uuid"])
  home_mounted = [m for m in list__row if Kernel.is__mount_point(Path("/home") / m["system_account__name"])]
  is__open = Path(f"/dev/mapper/{mapper}").exists()
  if not (record or home_mounted or is__open or Kernel.is__mount_point(dirpa__mount)):
    log.line("skipped" ,f"storage {name}" ,"not plugged in"); return

  # a file system left frozen would hold its processes from ending
  if Kernel.is__mount_point(dirpa__mount):
    file_system__thaw(dirpa__mount ,log ,preview)

  # 1. end every process of its accounts, whoever is at the console
  process__end([m["system_uid"] for m in list__row] ,log ,preview)

  # 2. unmount their homes, and record the detach while the storage can be read
  for m in home_mounted:
    dirpa__target = Path("/home") / m["system_account__name"]
    mount__remove(dirpa__target ,log ,preview)
  detach__record(system_db ,r ,log ,preview)

  # 3. delete the system accounts Account-Tree made for its accounts, keeping
  # what plugging it in again needs to make them again; free their uids
  count_of__unplugged = 0
  for m in list__row:
    if m["origin"] != "made": continue
    if not preview and Kernel.is__mount_point(Path("/home") / m["system_account__name"]):
      log.line("kept" ,m["system_account__name"] ,"its home is still mounted, so its system account is not deleted"); continue
    if SameAccount.same_account__unplug(system_db ,m ,log ,preview): count_of__unplugged += 1
  if count_of__unplugged and not preview:
    # accounts-daemon drops a deleted account from the login screen when it reloads
    run(["systemctl" ,"try-restart" ,"accounts-daemon.service"])

  # 4. hide its adopted accounts, which stay, from the login screen
  is__hidden__set_all(system_db ,r["database_uuid"] ,True ,log ,preview)

  # 5. unmount the storage and lock it
  if Kernel.is__mount_point(dirpa__mount):
    if not preview: os.sync()
    mount__remove(dirpa__mount ,log ,preview)
  if is__open:
    if run_logged(log ,preview ,mapper ,["cryptsetup" ,"close" ,mapper]):
      log.line("locked" ,f"storage {name}" ,f"{mapper} closed")
    elif run_logged(log ,preview ,mapper ,["cryptsetup" ,"close" ,"--deferred" ,mapper]):
      log.line("locked" ,f"storage {name}" ,f"{mapper} closes when the last file open on it is closed")

  # 6. power the drive off, so that its light going out says it is safe to pull
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
      fpa__plugged(name).unlink()
    except FileNotFoundError:
      pass

def set__process_uid(dirpa__pid):
  """
  The real, effective, saved and file system uids of a process.
  """
  try:
    for line in (dirpa__pid / "status").read_text().splitlines():
      if line.startswith("Uid:"):
        return {int(x) for x in line.split()[1:]}
  except (OSError ,ValueError):
    pass
  return set()

def is__zombie(dirpa__pid):
  """
  Whether a process has ended and waits only for its parent to collect its
  exit status. It holds no file and no mount, so it is not waited for.
  """
  try:
    stat_text = (dirpa__pid / "stat").read_text()
  except OSError:
    return True
  # the command name is in parentheses and can hold spaces; the state follows it
  return stat_text[stat_text.rindex(")") + 2:].startswith("Z")

def list__process(set__uid):
  result = []
  for p in Path("/proc").iterdir():
    if p.name.isdigit() and set__process_uid(p) & set__uid and not is__zombie(p):
      result.append(int(p.name))
  return result

def process__wait(set__uid ,second):
  deadline = time.monotonic() + second
  while time.monotonic() < deadline:
    if not list__process(set__uid): return []
    time.sleep(0.2)
  return list__process(set__uid)

def process__end(list__uid ,log ,preview):
  """
  End every process running under the uids: logind ends their sessions; what
  remains is sent SIGTERM, and what remains after that, SIGKILL.
  """
  set__uid = set(list__uid)
  if not set__uid: return
  pid = list__process(set__uid)
  if not pid:
    log.line("kept" ,"processes" ,"none runs under its accounts"); return
  if preview:
    log.line("ended" ,f"{len(pid)} processes" ,"of its accounts"); return
  for u in sorted(set__uid):
    run(["loginctl" ,"terminate-user" ,str(u)])
  remaining = process__wait(set__uid ,SESSION_WAIT__SECOND)
  for sig ,wait in ((signal.SIGTERM ,TERM_WAIT__SECOND) ,(signal.SIGKILL ,KILL_WAIT__SECOND)):
    if not remaining: break
    for p in remaining:
      try:
        os.kill(p ,sig)
      except ProcessLookupError:
        pass
    remaining = process__wait(set__uid ,wait)
  if remaining:
    log.error("processes" ,f"{len(remaining)} still run under its accounts: {', '.join(map(str ,remaining[:8]))}")
  else:
    log.line("ended" ,f"{len(pid)} processes" ,"of its accounts")

def mount__remove(dirp__target ,log ,preview):
  """
  Unmount target; when something still holds it open, detach it from the
  tree so that no one new can reach it, and the kernel finishes when the last
  holder lets go.
  """
  if preview:
    log.line("unmounted" ,str(dirp__target)); return True
  if Kernel.LIBC.umount2(os.fsencode(str(dirp__target)) ,0) == 0:
    log.line("unmounted" ,str(dirp__target)); return True
  if Kernel.LIBC.umount2(os.fsencode(str(dirp__target)) ,Kernel.MNT_DETACH) == 0:
    log.line("unmounted" ,str(dirp__target) ,"something held it open, so it was detached, and closes when that lets go")
    return True
  log.error(str(dirp__target) ,f"umount failed: {os.strerror(Kernel.ctypes.get_errno())}")
  return False

def detach__record(system_db ,r ,log ,preview):
  """
  Record the detach in both databases while the storage can still be read.
  When the drive has been pulled out first, the storage database cannot be
  written; the next attach finds the head it last recorded.
  """
  storage_db = None
  try:
    storage_db = Database.Database.open(Database.fpa__storage_database(dirpa__Account_Tree(r["dirpa__mount"])) ,"storage")
    SameAccount.system_seen__record(storage_db ,system_db.uuid() ,log ,preview)
    series = SameAccount.head_seen__record(system_db ,storage_db ,"detached" ,None ,preview)
    log.line("recorded" ,f"database {storage_db.short()} detached" ,f"head at series {series} remembered here")
  except (OSError ,sqlite3.Error ,DatabaseError) as error:
    log.line("skipped" ,f"storage {r['storage_name']}" ,f"the detach was not recorded, the storage cannot be read: {error}")
  finally:
    # an open database file would hold the storage's file system busy
    if storage_db is not None: storage_db.close()
