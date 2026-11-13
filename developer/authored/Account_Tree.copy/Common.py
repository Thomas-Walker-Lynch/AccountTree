# -*- mode: python; coding: utf-8; python-indent-offset: 2; indent-tabs-mode: nil -*-
"""
Common - what the work modules of Account-Tree_admin share: the log, carrying
out a change, and checking names and paths.

Each work function is given a log and a preview flag. Everything a function
does is decided the same way in a preview; the flag is tested only where a
change is made: a database transaction is rolled back, and a change to the
system is logged and not carried out.
"""

import os
import socket
from pathlib import Path

import Kernel
from Kernel import KernelError
from SystemAccount import SystemAccountError

DIRPA__HOME = Path("/home")
# a parent account's name and its child account's last name are joined by this
SEPARATOR = "·"
# a system account made for a branch of a storage database is named
# <name><BRANCH_MARK><eight hex digits>
BRANCH_MARK = "~"
DIRN__ACCOUNT_TREE = "Account-Tree"
# Where Account-Tree keeps its files on a storage, below the storage's root. This
# is the one place the layout is written; everything else asks
# dirpa__Account_Tree for it.
DIRR__ACCOUNT_TREE = Path("home") / DIRN__ACCOUNT_TREE
RESERVED_NAME = {DIRN__ACCOUNT_TREE}


class Log:
  TAG_WIDTH = 24

  # the tag a preview prints for each action tag
  PREVIEW_TAG = {
    "made": "would make" ,"recorded": "would record" ,"mounted": "would mount",
    "unmounted": "would unmount" ,"adopted": "would adopt",
    "made system account": "would make system account" ,"set": "would set" ,"deleted": "would delete",
    "deleted system account": "would delete system account" ,"deleted system group": "would delete system group",
    "retired": "would retire" ,"mapped": "would map" ,"branched": "would branch",
    "unlocked": "would unlock" ,"locked": "would lock" ,"powered off": "would power off",
    "ended": "would end" ,"watching": "would watch" ,"written": "would write" ,"reloaded": "would reload",
    "frozen": "would freeze" ,"thawed": "would thaw" ,"unplugged": "would unplug" ,"forgotten": "would forget",
  }

  def __init__(self ,preview):
    self.preview = preview
    self.count = {}
    self.count_of__error = 0

  def line(self ,tag ,subject ,reason=None):
    self.count[tag] = self.count.get(tag ,0) + 1
    if self.preview:
      tag = self.PREVIEW_TAG.get(tag ,tag)
    # a tag as long as the column, such as 'would delete system account', keeps two spaces after it
    text = f"  {tag:<{self.TAG_WIDTH}}" + ("  " if len(tag) >= self.TAG_WIDTH else "") + subject
    if reason: text += f"  ({reason})"
    print(text)

  def error(self ,subject ,reason):
    self.count_of__error += 1
    print(f"  {'error':<{self.TAG_WIDTH}}{subject}  ({reason})")

  def summary(self):
    print("summary:")
    for tag ,n in sorted(self.count.items()):
      if self.preview: tag = self.PREVIEW_TAG.get(tag ,tag)
      print(f"  {n:>4}  {tag}")
    if self.count_of__error:
      print(f"  {self.count_of__error:>4}  errors")
    if self.preview:
      print("preview only: nothing was changed.")
    return 1 if self.count_of__error else 0


def act(log ,preview ,subject ,function ,*argument):
  """
  Carry out a change to the system, function(*argument); in a preview, do
  nothing. Returns True when it was done, or would be; logs the error and
  returns False when it failed.
  """
  if preview:
    return True
  try:
    function(*argument)
    return True
  except (KernelError ,SystemAccountError ,OSError) as error:
    log.error(subject ,str(error))
    return False

def host_name() -> str:
  return socket.gethostname()

def valid_name(name):
  """
  Why name cannot be an account name, or None when it can.
  """
  if not name or len(name.encode("utf-8")) > 32:
    return "an account name is 1 to 32 bytes"
  if any(c in name for c in "/: \t\n") or name.startswith(("-" ,SEPARATOR)) or name.endswith(SEPARATOR):
    return "an account name may not contain '/', ':' or spaces, nor start with '-' or begin or end with '·'"
  if SEPARATOR * 2 in name:
    return "an account name may not contain an empty part between two '·'"
  if name in RESERVED_NAME:
    return f"'{name}' is reserved: /home/{name} holds Account-Tree's own files"
  return None

def dirpa__Account_Tree(dirp__storage):
  """
  The Account-Tree directory of the storage whose root is dirp__storage: the
  directory holding its storage database and account-data.
  """
  return Path(os.path.abspath(dirp__storage)) / DIRR__ACCOUNT_TREE

def dirpa__storage(dirp__Account_Tree):
  """The root of the storage whose Account-Tree directory is dirp__Account_Tree."""
  dirpa__each = Path(os.path.abspath(dirp__Account_Tree))
  for _ in DIRR__ACCOUNT_TREE.parts:
    dirpa__each = dirpa__each.parent
  return dirpa__each

def is__Account_Tree_directory(dirp):
  return Path(os.path.abspath(dirp)).parts[-len(DIRR__ACCOUNT_TREE.parts):] == DIRR__ACCOUNT_TREE.parts

def dirp__storage__problem(dirp__storage):
  """
  Why dirp__storage is not the root of a storage, or None. The root is the
  directory the storage is mounted at: / for the disk the system runs from.
  """
  if is__Account_Tree_directory(dirp__storage):
    return f"{dirp__storage} is the Account-Tree directory; give the storage's root, {dirpa__storage(dirp__storage)}"
  if not Path(dirp__storage).is_dir():
    return f"{dirp__storage} is not a directory; give a storage's root, such as / or /mnt/Hummingbird"
  return None

def delete_mount_point(dirp__mount_point ,log ,preview):
  """
  Delete a mount point only when it is one attach made: an empty directory
  owned by root, mode 0000. In a preview a home still mounted over it hides
  it, so the real run makes the check.
  """
  if not dirp__mount_point.exists():
    return
  if preview and Kernel.is__mount_point(dirp__mount_point):
    log.line("deleted" ,str(dirp__mount_point) ,"mount point, once unmounted, if attach made it")
    return
  st = dirp__mount_point.lstat()
  if Kernel.is__mount_point(dirp__mount_point) or not dirp__mount_point.is_dir() or st.st_uid != 0 or (st.st_mode & 0o7777) != 0 or any(dirp__mount_point.iterdir()):
    log.line("kept" ,str(dirp__mount_point) ,"not an empty mount point made by attach")
    return
  if not act(log ,preview ,str(dirp__mount_point) ,Kernel.immutable__set ,dirp__mount_point ,False): return
  if not act(log ,preview ,str(dirp__mount_point) ,dirp__mount_point.rmdir): return
  log.line("deleted" ,str(dirp__mount_point) ,"mount point")
