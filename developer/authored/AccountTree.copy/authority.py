# -*- mode: python; coding: utf-8; python-indent-offset: 2; indent-tabs-mode: nil -*-
"""
authority - the one place that answers "may this account do this?"

Every change AccountTree makes is first put to the authority as a request:
the acting account, the action, the target account, and the resource. Today there is
no authority program, so every request is refused with that reason, and
the window shows every change greyed out. Later this module becomes the
client side of the authority program's interface; the front ends do not
change.
"""

from dataclasses import dataclass


@dataclass
class Decision:
  allowed: bool
  reason: str


AUTHORITY_STATUS = "no authority program yet; every change is refused"

def request(actor: str ,action: str ,target: str ,resource: str = "") -> Decision:
  return Decision(False ,AUTHORITY_STATUS)
