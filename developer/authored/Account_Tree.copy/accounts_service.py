# -*- mode: python; coding: utf-8; python-indent-offset: 2; indent-tabs-mode: nil -*-
"""
accounts_service - whether an account is offered on the login screen

GDM, and the other login screens that use AccountsService, list the accounts
AccountsService reports. AccountsService keeps a file for each account it
knows, /var/lib/AccountsService/users/<name>, in the key file format:

  [User]
  Session=gnome
  SystemAccount=true

An account whose file says SystemAccount=true is left off the list; it can
still be logged into by typing its name. Account-Tree sets the key and leaves
every other key in the file as it was.

  is_hidden_set(name ,hidden)   set SystemAccount for one account

The directory has been /var/lib/AccountsService on Arch, Fedora and Debian;
one accountsservice release on Arch made /var/lib/accountsservice instead, so
both are looked for.
"""

import os
from pathlib import Path

DIRECTORY_LIST = (Path("/var/lib/AccountsService/users") ,Path("/var/lib/accountsservice/users"))
SECTION = "[User]"
KEY = "SystemAccount"


class AccountsServiceError(Exception):
  pass


def user_directory():
  """
  The directory AccountsService keeps its account files in, or None when this
  system has no AccountsService.
  """
  for d in DIRECTORY_LIST:
    if d.is_dir():
      return d
  return None

def text_with_key(text ,hidden):
  """
  The key file text with SystemAccount set in its [User] section, every other
  line kept. A file without a [User] section gets one.
  """
  value = "true" if hidden else "false"
  line_list = text.splitlines()
  result ,in_user ,done = [] ,False ,False
  for line in line_list:
    stripped = line.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
      if in_user and not done:
        # at the end of the [User] section, before the blank lines that end it
        at = len(result)
        while at > 0 and not result[at - 1].strip(): at -= 1
        result.insert(at ,f"{KEY}={value}"); done = True
      in_user = stripped == SECTION
      result.append(line); continue
    if in_user and stripped.split("=" ,1)[0].strip() == KEY:
      if not done:
        result.append(f"{KEY}={value}"); done = True
      continue
    result.append(line)
  if not done:
    if not any(l.strip() == SECTION for l in line_list):
      if result and result[-1].strip(): result.append("")
      result.append(SECTION)
    result.append(f"{KEY}={value}")
  return "\n".join(result) + "\n"

def is_hidden_set(name ,hidden):
  """
  Set SystemAccount=true (hidden) or false for the account. Returns whether
  the file changed, or None on a system without AccountsService.
  """
  d = user_directory()
  if d is None:
    return None
  path = d / name
  try:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    new_text = text_with_key(text ,hidden)
    if new_text == text:
      return False
    temporary = path.with_name(f".{name}.AccountTree")
    with open(temporary ,"w" ,encoding="utf-8") as f:
      f.write(new_text)
    os.chmod(temporary ,0o644)
    os.replace(temporary ,path)
  except OSError as error:
    raise AccountsServiceError(f"{path}: {error}")
  return True
