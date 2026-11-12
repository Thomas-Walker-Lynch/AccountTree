# -*- mode: python; coding: utf-8; python-indent-offset: 2; indent-tabs-mode: nil -*-
"""
ResourceKind - the kinds of resource an account can be allocated

Each resource kind is listed once here. The window builds its Allocate
panel from this list, so a kind that is listed but has no allocator yet
appears greyed out. Writing an allocator for a kind is the step that turns
its button on: set allocator to the function that opens its window.
"""

from dataclasses import dataclass
from typing import Callable ,Optional


@dataclass
class ResourceKind:
  name: str
  group: str
  description: str
  allocator: Optional[Callable] = None

  @property
  def implemented(self) -> bool:
    return self.allocator is not None


# Groups in the order the window shows them.
GROUP_ORDER = ["account" ,"storage" ,"network" ,"device" ,"privilege"]

GROUP_TITLE = {
  "account": "Accounts",
  "storage": "Storage",
  "network": "Network",
  "device": "Devices",
  "privilege": "Privileges",
}

LIST__KIND = [
  ResourceKind("child account" ,"account",
    "a new account below this one; it opens its own resource window"),
  ResourceKind("capture account" ,"account",
    "take an existing system account as a child account of this one; the authority decides whether this is allowed"),

  ResourceKind("home directory" ,"storage",
    "the account's own home, kept on a storage and mounted at /home/<account>"),
  ResourceKind("view" ,"storage",
    "an idmapped mount that shows a child account's files as owned by this account"),
  ResourceKind("storage" ,"storage",
    "a persistent storage device, removable or encrypted, this account may open and attach"),

  ResourceKind("network zone" ,"network",
    "a network namespace of the account's own; all of the account's traffic goes through it"),
  ResourceKind("WireGuard tunnel" ,"network",
    "a tunnel from the network zone to a remote machine"),
  ResourceKind("forwarded service" ,"network",
    "a local service (DNS, mail, ...) carried to the remote end of the tunnel, so it does not leak"),

  ResourceKind("display" ,"device",
    "permission to open windows on the parent account's display"),
  ResourceKind("sound card" ,"device",
    "the lease on the sound card; one account holds it at a time (the old subu_sound script does this today)"),
  ResourceKind("graphics chip" ,"device",
    "use of the GPU, or of a virtual slice of it"),
  ResourceKind("USB device" ,"device",
    "a particular USB device, by vendor, product and serial"),
  ResourceKind("udev device class" ,"device",
    "visibility of a class of devices, such as cameras or serial ports"),

  ResourceKind("sudo" ,"privilege",
    "permission to run commands as root, possibly limited to a list"),
  ResourceKind("mount" ,"privilege",
    "permission to mount file systems"),
]


def list__kind__by_group():
  result = []
  for group in GROUP_ORDER:
    result.append((group ,[k for k in LIST__KIND if k.group == group]))
  return result
