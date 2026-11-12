# -*- mode: python; coding: utf-8; python-indent-offset: 2; indent-tabs-mode: nil -*-
"""
kernel - direct calls to the Linux kernel and to libsystemd, made through
ctypes, for the work that Account-Tree does on mounts, files and sessions.

  home_mount            mount an account's home: a bind mount, idmapped when the
                        database uid and system uid differ, nosuid and nodev
  home_unmount          unmount it
  mount_point_list      every mount point, from /proc/self/mountinfo
  is_mount_point        whether a path is one
  immutable_set         set or clear a directory's immutable flag
  session_system_uid_set
                        the system uids that have a login session

home_mount uses the mount calls Linux has had since 5.12: open_tree clones the
source as a detached mount, mount_setattr gives it its attributes and its
idmap, and move_mount places it. An idmap is described to the kernel as a
user namespace: a child process is made in a new user namespace, its uid and
gid maps are written, and the namespace is handed to mount_setattr.
"""

import os ,re ,ctypes ,ctypes.util ,fcntl ,struct
from pathlib import Path

LIBC = ctypes.CDLL(None ,use_errno=True)

# system call numbers; these newer calls have the same numbers on every
# architecture Linux supports, x86_64 and aarch64 among them
SYS_open_tree = 428
SYS_move_mount = 429
SYS_mount_setattr = 442

AT_FDCWD = -100
AT_EMPTY_PATH = 0x1000
OPEN_TREE_CLONE = 1
OPEN_TREE_CLOEXEC = os.O_CLOEXEC
MOVE_MOUNT_F_EMPTY_PATH = 0x00000004
MOUNT_ATTR_NOSUID = 0x00000002
MOUNT_ATTR_NODEV = 0x00000004
MOUNT_ATTR_IDMAP = 0x00100000
MNT_DETACH = 0x00000002

FS_IOC_GETFLAGS = 0x80086601
FS_IOC_SETFLAGS = 0x40086602
FS_IMMUTABLE_FL = 0x00000010


class KernelError(Exception):
  pass


class MountAttr(ctypes.Structure):
  _fields_ = [("attr_set" ,ctypes.c_uint64) ,("attr_clr" ,ctypes.c_uint64),
              ("propagation" ,ctypes.c_uint64) ,("userns_fd" ,ctypes.c_uint64)]


def check(result ,what):
  if result < 0:
    number = ctypes.get_errno()
    raise KernelError(f"{what}: {os.strerror(number)}")
  return result


# --------------------------------------------------------------------------------
# mounts

def idmap_namespace_fd(database_uid ,system_uid ,system_gid):
  """
  A file descriptor for a user namespace whose maps say: a file owned on disk
  by database_uid appears as system_uid, and its group database_uid appears as
  system_gid. A uid_map line is '<id inside> <id outside> <count>'. For an
  idmapped mount the kernel takes the id on disk as an id inside the
  namespace, and the id it maps to outside as the id seen through the mount.
  """
  ready_read ,ready_write = os.pipe()
  release_read ,release_write = os.pipe()
  pid = os.fork()
  if pid == 0:
    try:
      os.close(ready_read); os.close(release_write)
      os.unshare(os.CLONE_NEWUSER)
      os.write(ready_write ,b"x")
      os.read(release_read ,1)
    finally:
      os._exit(0)
  os.close(ready_write); os.close(release_read)
  try:
    if os.read(ready_read ,1) != b"x":
      raise KernelError("the child process for the idmap did not start")
    Path(f"/proc/{pid}/uid_map").write_text(f"{database_uid} {system_uid} 1\n")
    Path(f"/proc/{pid}/gid_map").write_text(f"{database_uid} {system_gid} 1\n")
    return os.open(f"/proc/{pid}/ns/user" ,os.O_RDONLY | os.O_CLOEXEC)
  finally:
    os.write(release_write ,b"x")
    os.close(release_write); os.close(ready_read)
    os.waitpid(pid ,0)

def home_mount(source ,target ,database_uid ,system_uid ,system_gid):
  """
  Mount source at target, nosuid and nodev, translating database_uid to
  system_uid and system_gid unless they are the same.
  """
  tree_fd = check(LIBC.syscall(SYS_open_tree ,AT_FDCWD ,os.fsencode(str(source)),
                               OPEN_TREE_CLONE | OPEN_TREE_CLOEXEC) ,f"open_tree {source}")
  userns_fd = None
  try:
    attr = MountAttr()
    attr.attr_set = MOUNT_ATTR_NOSUID | MOUNT_ATTR_NODEV
    if not (database_uid == system_uid == system_gid):
      userns_fd = idmap_namespace_fd(database_uid ,system_uid ,system_gid)
      attr.attr_set |= MOUNT_ATTR_IDMAP
      attr.userns_fd = userns_fd
    check(LIBC.syscall(SYS_mount_setattr ,tree_fd ,b"" ,AT_EMPTY_PATH ,ctypes.byref(attr),
                       ctypes.sizeof(attr)) ,f"mount_setattr {source}")
    check(LIBC.syscall(SYS_move_mount ,tree_fd ,b"" ,AT_FDCWD ,os.fsencode(str(target)),
                       MOVE_MOUNT_F_EMPTY_PATH) ,f"move_mount {target}")
  finally:
    os.close(tree_fd)
    if userns_fd is not None: os.close(userns_fd)

def home_unmount(target):
  check(LIBC.umount2(os.fsencode(str(target)) ,0) ,f"umount2 {target}")

def mount_point_list():
  """
  Every mount point on the system, from /proc/self/mountinfo, where a space
  and a few other characters in a path are written as three-digit octal escapes.
  """
  result = []
  for line in Path("/proc/self/mountinfo").read_bytes().split(b"\n"):
    field = line.split(b" ")
    if len(field) > 4:
      raw = re.sub(rb"\\([0-7]{3})" ,lambda m: bytes([int(m.group(1) ,8)]) ,field[4])
      result.append(raw.decode("utf-8" ,"replace"))
  return result

def is_mount_point(path):
  return os.path.abspath(str(path)) in set(mount_point_list())


# --------------------------------------------------------------------------------
# files

def immutable_set(path ,on):
  """
  Set or clear the immutable flag of a file or directory, as chattr +i and
  chattr -i do: an immutable file cannot be changed, renamed or deleted, even
  by root, until the flag is cleared.
  """
  fd = os.open(str(path) ,os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
  try:
    buffer = bytearray(struct.pack("i" ,0))
    fcntl.ioctl(fd ,FS_IOC_GETFLAGS ,buffer)
    flag = struct.unpack("i" ,buffer)[0]
    flag = (flag | FS_IMMUTABLE_FL) if on else (flag & ~FS_IMMUTABLE_FL)
    fcntl.ioctl(fd ,FS_IOC_SETFLAGS ,struct.pack("i" ,flag))
  finally:
    os.close(fd)


# --------------------------------------------------------------------------------
# sessions

def session_system_uid_set():
  """
  The system uids that have a login session, from systemd-logind through
  sd_get_uids in libsystemd. Empty where logind is not running.
  """
  name = ctypes.util.find_library("systemd") or "libsystemd.so.0"
  try:
    libsystemd = ctypes.CDLL(name)
  except OSError:
    raise KernelError("libsystemd is not available, so login sessions cannot be read")
  pointer = ctypes.POINTER(ctypes.c_uint32)()
  count = libsystemd.sd_get_uids(ctypes.byref(pointer))
  if count < 0:
    return set()
  try:
    return {pointer[i] for i in range(count)}
  finally:
    LIBC.free(pointer)
