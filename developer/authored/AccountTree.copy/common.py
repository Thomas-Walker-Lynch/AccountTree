# -*- mode: python; coding: utf-8; python-indent-offset: 2; indent-tabs-mode: nil -*-
"""
common - what the work modules of AccountTree_admin share: the log, carrying
out a change, and checking names and paths.

Each work function is given a log and a preview flag. Everything a function
does is decided the same way in a preview; the flag is tested only where a
change is made: a database transaction is rolled back, and a change to the
system is logged and not carried out.
"""

import socket
from pathlib import Path

import kernel
from kernel import KernelError
from system_account import SystemAccountError

HOME = Path("/home")
# a parent account's name and its child account's last name are joined by this
SEPARATOR = "·"
# a system account made for a branch of a storage database is named
# <name><BRANCH_MARK><eight hex digits>
BRANCH_MARK = "~"
TREE_DIR_NAME = "AccountTree"
RESERVED_NAME = {TREE_DIR_NAME}


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
  }

  def __init__(self ,preview):
    self.preview = preview
    self.count = {}
    self.error_count = 0

  def line(self ,tag ,subject ,reason=None):
    self.count[tag] = self.count.get(tag ,0) + 1
    if self.preview:
      tag = self.PREVIEW_TAG.get(tag ,tag)
    text = f"  {tag:<{self.TAG_WIDTH}}{subject}"
    if reason: text += f"  ({reason})"
    print(text)

  def error(self ,subject ,reason):
    self.error_count += 1
    print(f"  {'error':<{self.TAG_WIDTH}}{subject}  ({reason})")

  def summary(self):
    print("summary:")
    for tag ,n in sorted(self.count.items()):
      if self.preview: tag = self.PREVIEW_TAG.get(tag ,tag)
      print(f"  {n:>4}  {tag}")
    if self.error_count:
      print(f"  {self.error_count:>4}  errors")
    if self.preview:
      print("preview only: nothing was changed.")
    return 1 if self.error_count else 0


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
    return f"'{name}' is reserved: /home/{name} holds AccountTree's own files"
  return None

def tree_dir_problem(tree_dir):
  """
  Why tree_dir is not a tree dir, or None. A tree dir is
  <storage root>/home/AccountTree; every storage keeps the same layout.
  """
  path = Path(tree_dir)
  if path.name != TREE_DIR_NAME or path.parent.name != "home":
    return f"{tree_dir} is not a tree dir; a tree dir is <storage root>/home/{TREE_DIR_NAME}, such as /home/{TREE_DIR_NAME}"
  return None

def delete_mount_point(target ,log ,preview):
  """
  Delete a mount point only when it is one attach made: an empty directory
  owned by root, mode 0000. In a preview a home still mounted over it hides
  it, so the real run makes the check.
  """
  if not target.exists():
    return
  if preview and kernel.is_mount_point(target):
    log.line("deleted" ,str(target) ,"mount point, once unmounted, if attach made it")
    return
  st = target.lstat()
  if kernel.is_mount_point(target) or not target.is_dir() or st.st_uid != 0 or (st.st_mode & 0o7777) != 0 or any(target.iterdir()):
    log.line("kept" ,str(target) ,"not an empty mount point made by attach")
    return
  if not act(log ,preview ,str(target) ,kernel.immutable_set ,target ,False): return
  if not act(log ,preview ,str(target) ,target.rmdir): return
  log.line("deleted" ,str(target) ,"mount point")
