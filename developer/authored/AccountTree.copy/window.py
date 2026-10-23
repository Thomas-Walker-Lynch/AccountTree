# -*- mode: python; coding: utf-8; python-indent-offset: 2; indent-tabs-mode: nil -*-
"""
window - the AccountTree resource window

One window shows the resources of one account, the window account, and acts
with the authority of another, the acting account: the account of the person
who started the tool. The first window is for the acting account. Clicking a
child account opens that child account's window, still acting with the first
account's authority.

The window is made of panels, each a framed area, so that every option is
visible at once without menus. An option that has no implementation yet is
shown greyed out. Pointing at any option shows what it does in a tooltip.

The window is drawn with GTK 4, the toolkit of the desktop's own programs,
so it opens on Wayland directly, or on X when that is what the session has.
"""

import account_tree
import authority
import resource_kind

GTK_MISSING = """\
AccountTree window: GTK 4 for Python is not available to {python}
  {error}
This Python needs the PyGObject package and the GTK 4 introspection data.
  System Python, openSUSE:  zypper install python313-gobject typelib-1_0-Gtk-4_0   (313 for Python 3.13)
  Project Python:           zypper install gobject-introspection-devel cairo-devel typelib-1_0-Gtk-4_0
                            python3 -m pip install PyGObject
"""

APPLICATION_ID = "com.ReasoningTechnology.AccountTree"
PAD = 6
STATE_REFRESH_SECONDS = 5


def load_gtk():
  # Draw with cairo, on the processor, and do not open OpenGL at all. A
  # window of buttons gains nothing from the graphics chip, and a child account
  # is often not allowed to open it, which makes GTK print a warning for every
  # attempt. GDK_DISABLE=gl is read by GTK 4.16 and later. Values already in
  # the environment win.
  import os
  os.environ.setdefault("GSK_RENDERER" ,"cairo")
  os.environ.setdefault("GDK_DISABLE" ,"gl")
  import gi
  gi.require_version("Gtk" ,"4.0")
  from gi.repository import Gtk ,Gio ,GLib
  return Gtk ,Gio ,GLib


# Panel borders drawn in the theme's own text colour at partial strength, so
# they stand out on a dark theme and a light one alike. The theme's default
# frame border is faint on dark themes.
PANEL_CSS = """
frame.account-tree-panel > border {
  border: 1px solid alpha(currentColor ,0.45);
  border-radius: 6px;
}
frame.account-tree-panel > label {
  font-weight: bold;
}
"""

def install_css(Gtk):
  from gi.repository import Gdk
  provider = Gtk.CssProvider()
  if hasattr(provider ,"load_from_string"):
    provider.load_from_string(PANEL_CSS)
  else:
    provider.load_from_data(PANEL_CSS ,-1)
  Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default() ,provider,
    Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def build_window_class(Gtk ,GLib):

  class PanelHelper:
    """
    The pieces both kinds of window are built from.
    """

    def panel(self ,title):
      """
      A framed panel and the box its contents go in.
      """
      frame = Gtk.Frame(label=title)
      frame.add_css_class("account-tree-panel")
      box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL ,spacing=PAD,
        margin_top=PAD ,margin_bottom=PAD ,margin_start=PAD ,margin_end=PAD)
      frame.set_child(box)
      return frame ,box

    def label(self ,text ,dim=False ,markup=False):
      widget = Gtk.Label(xalign=0 ,wrap=True)
      if markup:
        widget.set_markup(text)
      else:
        widget.set_text(text)
      if dim:
        widget.add_css_class("dim-label")
      return widget

    def option(self ,text ,description ,action=None):
      """
      A clickable option. Without an action it is greyed out, and its
      tooltip says it is not written yet.
      """
      button = Gtk.Button(label=text)
      if action is None:
        button.set_sensitive(False)
        button.set_tooltip_text(f"{description}\n[not written yet]")
      else:
        button.set_tooltip_text(description)
        button.connect("clicked" ,lambda b: action())
      return button

    def row(self ,widget_list):
      box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL ,spacing=PAD)
      for widget in widget_list:
        widget.set_hexpand(True)
        box.append(widget)
      return box

    def authority_line(self):
      """
      Says whose authority the window acts with, when it is not the window
      account's own.
      """
      if self.acting_account.name == self.window_account.name:
        return None
      return self.label(
        f"<i>acting with the authority of {escape(self.acting_account.name)}</i>" ,markup=True)


  class AllocateWindow(PanelHelper ,Gtk.Window):
    """
    The window for allocating resources of one group, such as Storage, to
    the window account. Each kind of resource in the group is a row: its
    button, which will open that kind's allocator, and what it is. Options
    a kind offers will be laid out here as allocators are written.
    """

    def __init__(self ,parent ,group ,window_account ,acting_account):
      super().__init__(transient_for=parent)
      self.window_account = window_account
      self.acting_account = acting_account
      title = resource_kind.GROUP_TITLE[group]
      self.set_title(f"AccountTree: {window_account.name}: allocate {title}")

      box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL ,spacing=PAD,
        margin_top=PAD ,margin_bottom=PAD ,margin_start=PAD ,margin_end=PAD)
      box.append(self.label(f"<b>{escape(window_account.name)}</b>" ,markup=True))
      line = self.authority_line()
      if line: box.append(line)

      frame ,panel = self.panel(f"Allocate {title}")
      grid = Gtk.Grid(column_spacing=PAD*2 ,row_spacing=PAD)
      kind_list = dict(resource_kind.kind_list_by_group())[group]
      for i ,kind in enumerate(kind_list):
        action = None
        if kind.implemented:
          action = lambda k=kind: k.allocator(self.window_account ,self.acting_account)
        grid.attach(self.option(kind.name ,kind.description ,action) ,0 ,i ,1 ,1)
        text = kind.description if kind.implemented else f"{kind.description}  (not written yet)"
        description = self.label(text ,dim=not kind.implemented)
        description.set_hexpand(True)
        grid.attach(description ,1 ,i ,1 ,1)
      panel.append(grid)
      box.append(frame)

      close = Gtk.Button(label="Close" ,halign=Gtk.Align.END)
      close.connect("clicked" ,lambda b: self.close())
      box.append(close)
      self.set_child(box)
      self.set_default_size(560 ,-1)


  class ResourceWindow(PanelHelper ,Gtk.ApplicationWindow):

    def __init__(self ,application ,window_account ,acting_account):
      super().__init__(application=application)
      self.window_account = window_account
      self.acting_account = acting_account
      self.set_title(f"AccountTree: {window_account.name}")

      grid = Gtk.Grid(column_spacing=PAD ,row_spacing=PAD,
        margin_top=PAD ,margin_bottom=PAD ,margin_start=PAD ,margin_end=PAD)
      grid.set_column_homogeneous(True)
      #   row 0   Account         Authority   Control
      #   row 1   Child account   Allocate    Allocated
      grid.attach(self.panel_identity() ,0 ,0 ,1 ,1)
      grid.attach(self.panel_authority(),1 ,0 ,1 ,1)
      grid.attach(self.panel_control()  ,2 ,0 ,1 ,1)
      grid.attach(self.panel_child_account() ,0 ,1 ,1 ,1)
      grid.attach(self.panel_allocate() ,1 ,1 ,1 ,1)
      grid.attach(self.panel_allocated(),2 ,1 ,1 ,1)

      # The panels can be taller than the screen allows a window to be. In a
      # scrolled window they keep their natural size and the window can be
      # any size, instead of GTK warning that the window is too short.
      scroller = Gtk.ScrolledWindow(propagate_natural_height=True ,propagate_natural_width=True)
      scroller.set_child(grid)
      self.set_child(scroller)

    # ----------------------------------------------------------------------------
    # panels

    def panel_identity(self):
      frame ,box = self.panel("Account")
      name = self.window_account.name
      parent = account_tree.parent_of(name)
      box.append(self.label(f"<big><b>{escape(name)}</b></big>" ,markup=True))
      if not parent:
        relation = "root, at the top of the account tree"
      elif parent == "root":
        relation = "login account, a child of root"
      else:
        relation = f"child account of {parent}"
      box.append(self.label(f"system uid {self.window_account.uid}   {relation}"))
      line = self.authority_line()
      if line: box.append(line)
      return frame

    def panel_allocate(self):
      """
      One button per group of resources. Each opens the window for
      allocating resources of that group.
      """
      frame ,box = self.panel("Allocate")
      for group ,kind_list in resource_kind.kind_list_by_group():
        button = Gtk.Button(label=resource_kind.GROUP_TITLE[group])
        button.set_tooltip_text(", ".join(kind.name for kind in kind_list))
        button.connect("clicked" ,lambda b ,g=group: self.open_allocate(g))
        box.append(button)
      return frame

    def panel_child_account(self):
      """
      One row per child account of the window account: a button with its
      short name, which opens its window, and its state. The states are read
      again every few seconds, so a session starting or ending shows without
      reopening the window.
      """
      frame ,box = self.panel("Child account")
      child_list = account_tree.child_account_list(self.window_account.name)
      if not child_list:
        box.append(self.label("(none)" ,dim=True))
        return frame
      grid = Gtk.Grid(column_spacing=PAD*2 ,row_spacing=PAD)
      self.child_state_label = []
      session_set = account_tree.session_system_uid_set()
      for i ,child in enumerate(child_list):
        button = Gtk.Button(label=account_tree.short_name(child.name))
        button.get_child().set_xalign(0)
        button.set_hexpand(True)
        button.set_tooltip_text(
          f"open the resource window of {child.name}, "
          f"acting with the authority of {self.acting_account.name}")
        button.connect("clicked" ,lambda b ,c=child: self.open_child_account(c))
        grid.attach(button ,0 ,i ,1 ,1)
        state = self.label(account_tree.state(child ,session_set))
        grid.attach(state ,1 ,i ,1 ,1)
        self.child_state_label.append((child ,state))
      box.append(grid)
      GLib.timeout_add_seconds(STATE_REFRESH_SECONDS ,self.refresh_child_state)
      return frame

    def refresh_child_state(self):
      session_set = account_tree.session_system_uid_set()
      for child ,label in self.child_state_label:
        label.set_text(account_tree.state(child ,session_set))
      return self.get_visible()       # False stops the timer once the window is closed

    def panel_allocated(self):
      frame ,box = self.panel("Allocated")
      window_account = self.window_account
      state = "present" if window_account.home_exists else "not found"
      box.append(self.label(f"home   {window_account.home}   ({state})"))
      box.append(self.label("other resources: there is no resource database yet" ,dim=True))
      return frame

    def panel_control(self):
      """
      What can be done to the window account as a whole: its sessions, and
      its life.
      """
      frame ,box = self.panel("Control")
      box.append(self.row([
        self.option("enter",
          "open a shell or desktop session in this account (the old subu_login script does this today)"),
        self.option("end sessions" ,"end every session and service of this account"),
        self.option("take sound",
          "give this account the sound card lease (the old subu_sound script does this today)"),
      ]))
      box.append(self.row([
        self.option("rename" ,"give this account a new name; its uid and files stay"),
        self.option("deprecate" ,"lock this account and end its sessions; its data and name are kept"),
        self.option("deallocate",
          "remove this account once deprecated; its data is archived or destroyed and its uid retired"),
      ]))
      return frame

    def panel_authority(self):
      frame ,box = self.panel("Authority")
      acting = self.acting_account
      box.append(self.label(f"acting account   {acting.name}   (system uid {acting.uid})"))
      decision = authority.request(acting.name ,"view" ,self.window_account.name)
      box.append(self.label(f"authority   {decision.reason}" ,dim=True))
      return frame

    # ----------------------------------------------------------------------------
    # actions

    def open_allocate(self ,group):
      AllocateWindow(self ,group ,self.window_account ,self.acting_account).present()

    def open_child_account(self ,child):
      ResourceWindow(self.get_application() ,child ,self.acting_account).present()

  return ResourceWindow


def escape(text):
  return text.replace("&" ,"&amp;").replace("<" ,"&lt;").replace(">" ,"&gt;")


def run(window_account ,acting_account):
  import sys
  try:
    Gtk ,Gio ,GLib = load_gtk()
  except (ImportError ,ValueError) as error:
    print(GTK_MISSING.format(python=sys.executable ,error=error) ,file=sys.stderr ,end="")
    return 1

  ResourceWindow = build_window_class(Gtk ,GLib)
  install_css(Gtk)
  application = Gtk.Application(application_id=APPLICATION_ID,
    flags=Gio.ApplicationFlags.NON_UNIQUE)
  application.connect("activate",
    lambda app: ResourceWindow(app ,window_account ,acting_account).present())
  return application.run([])
