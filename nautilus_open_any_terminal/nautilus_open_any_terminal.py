"""nautilus extension: nautilus_open_any_terminal"""

# based on: https://github.com/gnunn1/tilix/blob/master/data/nautilus/open-tilix.py

import ast
import re
import shlex
from dataclasses import dataclass, field
from functools import cache
from gettext import gettext, translation
from os.path import expanduser
from subprocess import Popen
from typing import Optional
from urllib.parse import quote, unquote, urlparse

from gi import get_required_version, require_version

API_VERSION: str
if (API_VERSION := get_required_version("Nautilus")) is not None:
    try:
        require_version("Gtk", "4.0")
    except ValueError:
        require_version("Gtk", "3.0")
    from gi.repository import Nautilus as FileManager
elif (API_VERSION := get_required_version("Caja")) is not None:
    require_version("Gtk", "3.0")
    from gi.repository import Caja as FileManager
else:
    raise RuntimeError("This module can only be executed as a Nautilus/Caja extension")

from gi.repository import Gio, GLib, GObject, Gtk  # noqa: E402 pylint: disable=wrong-import-position


@dataclass(frozen=True)
class Terminal:
    """Data class representing a terminal configuration."""

    name: str
    workdir_arguments: Optional[list[str]] = None
    new_tab_arguments: Optional[list[str]] = None
    new_window_arguments: Optional[list[str]] = None
    command_arguments: list[str] = field(default_factory=lambda: ["-e"])
    flatpak_package: Optional[str] = None


_ = gettext
for localedir in [expanduser("~/.local/share/locale"), "/usr/share/locale"]:
    try:
        trans = translation("nautilus-open-any-terminal", localedir)
        trans.install()
        _ = trans.gettext
        break
    except FileNotFoundError:
        continue

TERMINALS = {
    "alacritty": Terminal("Alacritty"),
    "app2unit-term": Terminal("app2unit-term"),
    "blackbox": Terminal(
        "Black Box",
        workdir_arguments=["--working-directory"],
        command_arguments=["-c"],
        flatpak_package="com.raggesilver.BlackBox",
    ),
    "blackbox-terminal": Terminal(
        "Black Box",
        workdir_arguments=["--working-directory"],
        command_arguments=["-c"],
    ),
    "bobcat": Terminal(
        "Bobcat",
        workdir_arguments=["--working-dir"],
        command_arguments=["--"],
    ),
    "cool-retro-term": Terminal("cool-retro-term", workdir_arguments=["--workdir"]),
    "custom": Terminal(_("Terminal"), command_arguments=[]),
    "contour": Terminal(
        "Contour",
        workdir_arguments=["--working-directory"],
        flatpak_package="org.contourterminal.Contour",
    ),
    "cosmic-term": Terminal("COSMIC Terminal"),
    "deepin-terminal": Terminal("Deepin Terminal"),
    "ddterm": Terminal(
        "Drop down Terminal extension",
        workdir_arguments=["--working-directory"],
        flatpak_package="com.github.amezin.ddterm",
    ),
    "foot": Terminal("Foot"),
    "footclient": Terminal("FootClient"),
    "ghostty": Terminal("Ghostty"),
    "gnome-terminal": Terminal("Terminal", new_tab_arguments=["--tab"], command_arguments=["--"]),
    "guake": Terminal("Guake", workdir_arguments=["--show", "--new-tab"]),
    "kermit": Terminal("Kermit"),
    "kgx": Terminal("Console", new_tab_arguments=["--tab"]),
    "kitty": Terminal("Kitty"),
    "konsole": Terminal("Konsole", new_tab_arguments=["--new-tab"]),
    "mate-terminal": Terminal("Mate Terminal", new_tab_arguments=["--tab"]),
    "mlterm": Terminal("Mlterm"),
    "ptyxis": Terminal(
        "Ptyxis",
        workdir_arguments=["-d"],
        command_arguments=["--"],
        new_tab_arguments=["--tab"],
        new_window_arguments=["--new-window"],
        flatpak_package="app.devsuite.Ptyxis",
    ),
    "ptyxis-nightly": Terminal(
        "Ptyxis",
        workdir_arguments=["-d"],
        command_arguments=["--"],
        new_tab_arguments=["--tab"],
        new_window_arguments=["--new-window"],
        flatpak_package="org.gnome.Ptyxis.Devel",
    ),
    "qterminal": Terminal("QTerminal"),
    "rio": Terminal("Rio"),
    "sakura": Terminal("Sakura"),
    "st": Terminal("Simple Terminal"),
    "tabby": Terminal("Tabby", command_arguments=["run"], workdir_arguments=["open"]),
    "terminator": Terminal("Terminator", new_tab_arguments=["--new-tab"]),
    "terminology": Terminal("Terminology"),
    "terminus": Terminal("Terminus"),
    "termite": Terminal("Termite"),
    "tilix": Terminal("Tilix", flatpak_package="com.gexperts.Tilix"),
    "urxvt": Terminal("rxvt-unicode"),
    "urxvtc": Terminal("urxvtc"),
    "uwsm-terminal": Terminal("uwsm-terminal"),
    "uxterm": Terminal("UXTerm"),
    "warp": Terminal(
        "Warp",
        new_tab_arguments=["--virtual-arg-for-tabs"],  # This is just to indicate tab support
    ),
    "wezterm": Terminal(
        "Wez's Terminal Emulator",
        workdir_arguments=["--cwd"],
        new_tab_arguments=["start", "--new-tab"],
        new_window_arguments=["start"],
        flatpak_package="org.wezfurlong.wezterm",
    ),
    "xfce4-terminal": Terminal("Xfce Terminal", new_tab_arguments=["--tab"]),
    "xterm": Terminal("XTerm"),
}

FLATPAK_PARMS = ["off", "system", "user"]

@dataclass
class TerminalConfig:
    """A resolved terminal entry: the terminal id, its static definition, and its launch command prefix."""

    terminal: str
    data: Terminal
    cmd: list[str]


terminal_configs: list[TerminalConfig] = []
new_tab = False
flatpak = FLATPAK_PARMS[0]
custom_local_command: str = ""
custom_remote_command: str = ""

GSETTINGS_PATH = "com.github.stunkymonkey.nautilus-open-any-terminal"
GSETTINGS_KEYBINDINGS = "keybindings"
GSETTINGS_BIND_REMOTE = "bind-remote"
GSETTINGS_TERMINAL = "terminal"
GSETTINGS_TERMINALS = "terminals"
GSETTINGS_NEW_TAB = "new-tab"
GSETTINGS_FLATPAK = "flatpak"
GSETTINGS_USE_GENERIC_TERMINAL_NAME = "use-generic-terminal-name"
GSETTINGS_CUSTOM_LOCAL_COMMAND = "custom-local-command"
GSETTINGS_CUSTOM_REMOTE_COMMAND = "custom-remote-command"
REMOTE_URI_SCHEME = ["ftp", "sftp"]


# Adapted from https://www.freedesktop.org/software/systemd/man/latest/os-release.html
def read_os_release():
    """Read and parse the OS release information."""
    possible_os_release_paths = ["/etc/os-release", "/usr/lib/os-release"]
    for file_path in possible_os_release_paths:
        try:
            with open(file_path, mode="r", encoding="utf-8") as os_release:
                for line_number, line in enumerate(os_release, start=1):
                    line = line.rstrip()
                    if not line or line.startswith("#"):
                        continue
                    result = re.match(r"([A-Z][A-Z_0-9]+)=(.*)", line)
                    if result:
                        name, val = result.groups()
                        if val and val[0] in "\"'":
                            val = ast.literal_eval(val)
                        yield name, val
                    else:
                        raise OSError(f"{file_path}:{line_number}: bad line {line!r}")
        except FileNotFoundError:
            continue


@cache
def distro_id() -> set[str]:
    """get the set of distribution ids"""
    try:
        os_release = dict(read_os_release())
    except OSError:
        return set(["unknown"])
    ids = [os_release["ID"]]
    if id_like := os_release.get("ID_LIKE"):
        ids.extend(id_like.split(" "))
    return set(ids)


def parse_custom_command(command: str, data: str | list[str]) -> list[str]:
    """Substitute every '%s' in the command with data and split it into arguments"""
    if isinstance(data, str):
        data = [data]

    return shlex.split(command.replace("%s", shlex.join(data)))


def run_command_in_terminal(config: TerminalConfig, command: list[str], *, cwd: str | None = None):
    if config.terminal == "custom":
        cmd = parse_custom_command(custom_remote_command, command)
    else:
        cmd = config.cmd.copy()
        # Remove '--new-window' argument for Ptyxis remote sessions (fixes window size reset)
        if config.terminal == "ptyxis" and (command and command[0] == "ssh"):
            del cmd[1]
        if cwd and config.data.workdir_arguments:
            cmd.extend(config.data.workdir_arguments)
            cmd.append(cwd)
        cmd.extend(config.data.command_arguments)
        cmd.extend(command)

    Popen(cmd, cwd=cwd)  # pylint: disable=consider-using-with


def ssh_command_from_uri(uri: str, *, is_directory: bool):
    """Creates an ssh command that executes or cd's into remote uri"""
    result = urlparse(uri)
    cmd = ["ssh", "-t"]
    if result.username:
        cmd.append(f"{result.username}@{result.hostname}")
    else:
        cmd.append(result.hostname)  # type: ignore

    if result.port:
        cmd.append("-p")
        cmd.append(str(result.port))

    target = shlex.quote(unquote(result.path))
    if is_directory:
        cmd.extend(["cd", target, ";", "exec", "${SHELL:-/bin/sh}", "-l"])
    else:
        cmd.extend(["exec", target])

    return cmd


def open_remote_terminal_in_uri(config: TerminalConfig, uri: str):
    """Open a new remote terminal"""
    run_command_in_terminal(config, ssh_command_from_uri(uri, is_directory=True))


def open_local_terminal_in_uri(config: TerminalConfig, uri: str):
    """open the new terminal with correct path"""
    result = urlparse(uri)

    # For remote URIs (sftp/ftp), get the GVFS mount path instead of parsing the URI
    if result.scheme in REMOTE_URI_SCHEME:
        gfile = Gio.File.new_for_uri(uri)
        filename = gfile.get_path()
        if not filename:
            print(f"open-any-terminal: Could not get local path for {uri}")
            return
    else:
        filename = unquote(result.path)

    if result.scheme == "admin":
        run_command_in_terminal(config, ["sudo", "-s"], cwd=filename)
        return

    if config.terminal == "warp":
        # Force new_tab to be considered even without traditional tab arguments
        Popen(  # pylint: disable=consider-using-with
            ["xdg-open", f"warp://action/new_{'tab' if new_tab else 'window'}?path={result.path}"]
        )
        return

    cmd = config.cmd.copy()
    if config.terminal == "custom":
        cmd = parse_custom_command(custom_local_command, filename)
    elif filename and config.data.workdir_arguments:
        cmd.extend(config.data.workdir_arguments)
        cmd.append(filename)

    Popen(cmd, cwd=filename)  # pylint: disable=consider-using-with


def directory_menu_item_id(*, foreground: bool, remote: bool, terminal_id: str):
    return (
        f"OpenTerminal::open{'_' if foreground else '_bg_'}"
        f"{'remote' if remote else 'file'}_{terminal_id}_item"
    )


def executable_menu_item_id(*, remote: bool, terminal_id: str):
    return f"OpenTerminal::execute{'_remote_' if remote else '_file_'}{terminal_id}_item"


def get_directory_menu_items_for(
    config: TerminalConfig,
    file: FileManager.FileInfo,
    callback,
    *,
    foreground: bool,
    terminal_name: str | None = None,
):
    items = []
    remote = file.get_uri_scheme() in REMOTE_URI_SCHEME
    terminal_name = terminal_name or config.data.name

    if remote:
        if foreground:
            REMOTE_LABEL = _("Open in Remote {}")
            REMOTE_TIP = _("Open Remote {} in {}")
            LOCAL_LABEL = _("Open in Local {}")
            LOCAL_TIP = _("Open Local {} in {}")
            tip = REMOTE_TIP.format(terminal_name, file.get_name())
        else:
            REMOTE_LABEL = _("Open Remote {} Here")
            REMOTE_TIP = _("Open Remote {} in This Directory")
            LOCAL_LABEL = _("Open Local {} Here")
            LOCAL_TIP = _("Open Local {} in This Directory")
            tip = REMOTE_TIP.format(terminal_name)

        item = FileManager.MenuItem(
            name=directory_menu_item_id(foreground=foreground, remote=True, terminal_id=config.terminal),
            label=REMOTE_LABEL.format(terminal_name),
            tip=tip,
        )
        item.connect("activate", callback, file, True, config)
        items.append(item)
    elif foreground:
        LOCAL_LABEL = _("Open in {}")
        LOCAL_TIP = _("Open {} in {}")
    else:
        LOCAL_LABEL = _("Open {} Here")
        LOCAL_TIP = _("Open {} in This Directory")

    # Let wezterm handle opening a local terminal
    if config.terminal == "wezterm" and flatpak == "off":
        return items

    if foreground:
        tip = LOCAL_TIP.format(terminal_name, file.get_name())
    else:
        tip = LOCAL_TIP.format(terminal_name)

    item = FileManager.MenuItem(
        name=directory_menu_item_id(foreground=foreground, remote=False, terminal_id=config.terminal),
        label=LOCAL_LABEL.format(terminal_name),
        tip=tip,
    )
    item.connect("activate", callback, file, False, config)
    items.append(item)
    return items


def get_directory_menu_items(
    file: FileManager.FileInfo, callback, *, foreground: bool, terminal_name: str | None = None
):
    """Aggregate directory menu items across all configured terminals."""
    items = []
    for config in terminal_configs:
        items.extend(
            get_directory_menu_items_for(
                config, file, callback, foreground=foreground, terminal_name=terminal_name
            )
        )
    return items


def get_executable_menu_items_for(
    config: TerminalConfig,
    file: FileManager.FileInfo,
    callback,
    *,
    terminal_name: str | None = None,
):
    items = []
    remote = file.get_uri_scheme() in REMOTE_URI_SCHEME
    terminal_name = terminal_name or config.data.name

    if remote:
        REMOTE_LABEL = _("Execute in Remote {}")
        REMOTE_TIP = _("Execute {} in {} via SSH")
        LOCAL_LABEL = _("Execute in Local {}")
        LOCAL_TIP = _("Execute {} in Local {}")

        tip = REMOTE_TIP.format(file.get_name(), terminal_name)
        item = FileManager.MenuItem(
            name=executable_menu_item_id(remote=True, terminal_id=config.terminal),
            label=REMOTE_LABEL.format(terminal_name),
            tip=tip,
        )
        item.connect("activate", callback, file, True, config)
        items.append(item)
    else:
        LOCAL_LABEL = _("Execute in {}")
        LOCAL_TIP = _("Execute {} in {}")

    tip = LOCAL_TIP.format(file.get_name(), terminal_name)
    item = FileManager.MenuItem(
        name=executable_menu_item_id(remote=False, terminal_id=config.terminal),
        label=LOCAL_LABEL.format(terminal_name),
        tip=tip,
    )
    item.connect("activate", callback, file, False, config)
    items.append(item)
    return items


def get_executable_menu_items(file: FileManager.FileInfo, callback, *, terminal_name: str | None = None):
    """Aggregate executable menu items across all configured terminals."""
    items = []
    for config in terminal_configs:
        items.extend(
            get_executable_menu_items_for(config, file, callback, terminal_name=terminal_name)
        )
    return items


def is_executable(file: Gio.File) -> bool:
    try:
        attributes = file.query_info("access::can-execute", Gio.FileQueryInfoFlags.NONE)
    except GLib.Error:
        return False
    return attributes.get_attribute_boolean("access::can-execute")


def _migrate_legacy_terminal_key():
    """If the deprecated 'terminal' key is set, push it into 'terminals' and clear it."""
    legacy = _gsettings.get_string(GSETTINGS_TERMINAL)
    if not legacy:
        return
    current = list(_gsettings.get_strv(GSETTINGS_TERMINALS))
    if legacy not in current:
        # Prepend so the previously-selected terminal remains the primary one (for the keybinding).
        current.insert(0, legacy)
        _gsettings.set_strv(GSETTINGS_TERMINALS, current)
    _gsettings.set_string(GSETTINGS_TERMINAL, "")
    print(f"open-any-terminal: migrated legacy 'terminal=\"{legacy}\"' into 'terminals'")


def _build_terminal_config(terminal_id: str, *, new_tab_pref: bool) -> TerminalConfig | None:
    """Resolve a single terminal id into a runnable TerminalConfig, or return None if unknown."""
    data = TERMINALS.get(terminal_id)
    if not data:
        print(f'open-any-terminal: unknown terminal "{terminal_id}"')
        return None

    if flatpak != FLATPAK_PARMS[0] and data.flatpak_package is not None:
        cmd = ["flatpak", "run", "--" + flatpak, data.flatpak_package]
    else:
        cmd = [terminal_id]
        if terminal_id == "blackbox" and "fedora" in distro_id():
            cmd[0] = "blackbox-terminal"

    if terminal_id == "custom":
        cmd = []
    elif new_tab_pref and data.new_tab_arguments:
        cmd.extend(data.new_tab_arguments)
    elif data.new_window_arguments:
        cmd.extend(data.new_window_arguments)

    return TerminalConfig(terminal=terminal_id, data=data, cmd=cmd)


def set_terminal_args(*_args):
    # pylint: disable=possibly-used-before-assignment
    """Rebuild the list of TerminalConfig from the current GSettings."""
    global new_tab
    global flatpak
    global terminal_configs
    global custom_local_command
    global custom_remote_command

    _migrate_legacy_terminal_key()

    newer_tab = _gsettings.get_boolean(GSETTINGS_NEW_TAB)
    flatpak = FLATPAK_PARMS[_gsettings.get_enum(GSETTINGS_FLATPAK)]
    new_tab = newer_tab

    custom_local_command = _gsettings.get_string(GSETTINGS_CUSTOM_LOCAL_COMMAND)
    custom_remote_command = _gsettings.get_string(GSETTINGS_CUSTOM_REMOTE_COMMAND)

    terminal_ids = list(_gsettings.get_strv(GSETTINGS_TERMINALS))
    if not terminal_ids:
        terminal_ids = ["gnome-terminal"]

    configs: list[TerminalConfig] = []
    for terminal_id in terminal_ids:
        config = _build_terminal_config(terminal_id, new_tab_pref=newer_tab)
        if config is not None:
            configs.append(config)

    terminal_configs = configs

    summary = ", ".join(c.terminal for c in terminal_configs) or "<none>"
    flatpak_text = f"flatpak={flatpak}" if flatpak != FLATPAK_PARMS[0] else ""
    tab_text = "new tab" if newer_tab else "new window"
    print(f"open-any-terminal: terminals={summary} ({tab_text}) {flatpak_text}")


if API_VERSION == "4.0":

    class OpenAnyTerminalShortcutProvider(GObject.GObject, FileManager.MenuProvider):
        """Provide keyboard shortcuts for opening terminals in Nautilus."""

        def __init__(self):
            super().__init__()
            self.previous_cwd = expanduser("~")

            gsettings_source = Gio.SettingsSchemaSource.get_default()
            if gsettings_source.lookup(GSETTINGS_PATH, True):
                self._gsettings = Gio.Settings.new(GSETTINGS_PATH)
                self._setup_keybindings()

        def get_background_items(self, current_folder: FileManager.FileInfo):
            """Update current URI when folder changes."""
            if current_folder:
                if current_folder.get_uri_scheme() in REMOTE_URI_SCHEME:
                    folder_path = current_folder.get_uri()
                else:
                    folder_path = current_folder.get_location().get_path()

                if folder_path and folder_path != self.previous_cwd:
                    self.previous_cwd = folder_path
            return []

        def _open_terminal(self, *_args):
            """Open the primary terminal (first entry in the list) at the specified URI."""
            if not terminal_configs:
                return
            primary = terminal_configs[0]
            if self._gsettings.get_boolean(GSETTINGS_BIND_REMOTE):
                open_remote_terminal_in_uri(primary, self.previous_cwd)
            else:
                open_local_terminal_in_uri(primary, self.previous_cwd)

        def _setup_keybindings(self):
            """Set up custom keybindings for the extension."""
            self.app = Gtk.Application.get_default()
            if self.app is None:
                print("No Gtk.Application found. Keybindings cannot be set.")
                return

            action = Gio.SimpleAction.new("open_any_terminal", None)
            action.connect("activate", self._open_terminal)
            self.app.add_action(action)
            self._bind_shortcut()
            self._gsettings.connect("changed", self._update_shortcut)

        def _update_shortcut(self, _gsettings, key):
            """remove keybinding"""
            if key == GSETTINGS_KEYBINDINGS:
                self.app.set_accels_for_action("app.open_any_terminal", [])
                self._bind_shortcut()

        def _bind_shortcut(self):
            """Parse and update keybindings when settings change."""
            shortcut = self._gsettings.get_string(GSETTINGS_KEYBINDINGS)

            if not shortcut:
                self.app.set_accels_for_action("app.open_any_terminal", [])
                return

            valid, key, mods = Gtk.accelerator_parse(shortcut)
            if not valid:
                print("Invalid shortcut in GSettings: %r", shortcut)
                self.app.set_accels_for_action("app.open_any_terminal", [])
                return

            normalized = Gtk.accelerator_name(key, mods)
            self.app.set_accels_for_action("app.open_any_terminal", [normalized])

elif API_VERSION in ("3.0", "2.0"):

    class OpenAnyTerminalShortcutProviderLegacy(GObject.GObject, FileManager.LocationWidgetProvider):
        """Provide keyboard shortcuts for opening terminals in Nautilus/Caja."""

        def __init__(self):
            super().__init__()
            gsettings_source = Gio.SettingsSchemaSource.get_default()
            if gsettings_source.lookup(GSETTINGS_PATH, True):
                self._gsettings = Gio.Settings.new(GSETTINGS_PATH)
                self._gsettings.connect("changed", self._bind_shortcut)
                self._create_accel_group()
            self._window = None
            self._uri = None

        def _create_accel_group(self):
            self._accel_group = Gtk.AccelGroup()
            shortcut = self._gsettings.get_string(GSETTINGS_KEYBINDINGS)
            key, mod = Gtk.accelerator_parse(shortcut)
            self._accel_group.connect(key, mod, Gtk.AccelFlags.VISIBLE, self._open_terminal)

        def _bind_shortcut(self, _gsettings, key):
            if key == GSETTINGS_KEYBINDINGS:
                self._accel_group.disconnect(self._open_terminal)
                self._create_accel_group()

        def _open_terminal(self, *_args):
            if not terminal_configs:
                return
            primary = terminal_configs[0]
            if _gsettings.get_boolean(GSETTINGS_BIND_REMOTE):
                open_local_terminal_in_uri(primary, self._uri)
            else:
                open_remote_terminal_in_uri(primary, self._uri)

        def get_widget(self, uri, window):
            """follows uri and sets the correct window"""
            self._uri = uri
            if self._window:
                self._window.remove_accel_group(self._accel_group)
            if self._gsettings:
                window.add_accel_group(self._accel_group)
            self._window = window


class OpenAnyTerminalExtension(GObject.GObject, FileManager.MenuProvider):
    """Provide context menu items for opening terminals in Nautilus."""

    def __init__(self):
        super().__init__()
        gsettings_source = Gio.SettingsSchemaSource.get_default()
        if gsettings_source.lookup(GSETTINGS_PATH, True):
            self._gsettings = Gio.Settings.new(GSETTINGS_PATH)

    def _get_terminal_name(self):
        if self._gsettings.get_boolean(GSETTINGS_USE_GENERIC_TERMINAL_NAME):
            return _("Terminal")
        return None

    def _menu_dir_activate_cb(self, menu, file_, remote: bool, config: TerminalConfig):
        if remote:
            open_remote_terminal_in_uri(config, file_.get_uri())
        else:
            if file_.get_uri_scheme() == "smb":
                file_uri = "file://" + quote(file_.get_location().get_path())
            else:
                file_uri = file_.get_uri()
            open_local_terminal_in_uri(config, file_uri)

    def _menu_exe_activate_cb(self, menu, file_, remote: bool, config: TerminalConfig):
        if remote:
            cmd = ssh_command_from_uri(file_.get_uri(), is_directory=False)
        else:
            result = urlparse(file_.get_uri())
            file = unquote(result.path)

            if result.scheme == "admin":
                cmd = ["sudo", file]
            elif config.terminal in ["xterm", "uxterm"]:
                cmd = [f"exec {shlex.quote(file)}"]
            else:
                cmd = [file]
        run_command_in_terminal(config, cmd)

    def get_file_items(self, *args):
        """Generates a list of menu items for a file or folder in the Nautilus file manager."""
        # `args` will be `[files: List[Nautilus.FileInfo]]` in Nautilus 4.0 API,
        # and `[window: Gtk.Widget, files: List[Nautilus.FileInfo]]` in Nautilus 3.0 API.

        files = args[-1]

        if len(files) != 1:
            return []
        file_ = files[0]

        if file_.is_directory():
            return get_directory_menu_items(
                file_, self._menu_dir_activate_cb, foreground=True, terminal_name=self._get_terminal_name()
            )

        if is_executable(file_.get_location()):
            return get_executable_menu_items(file_, self._menu_exe_activate_cb, terminal_name=self._get_terminal_name())

        return []

    def get_background_items(self, *args):
        """Generates a list of background menu items for a file or folder in the Nautilus file manager."""
        # `args` will be `[folder: Nautilus.FileInfo]` in Nautilus 4.0 API,
        # and `[window: Gtk.Widget, file: Nautilus.FileInfo]` in Nautilus 3.0 API.

        file_ = args[-1]
        return get_directory_menu_items(
            file_, self._menu_dir_activate_cb, foreground=False, terminal_name=self._get_terminal_name()
        )


source = Gio.SettingsSchemaSource.get_default()
if source is not None and source.lookup(GSETTINGS_PATH, True):
    _gsettings = Gio.Settings.new(GSETTINGS_PATH)
    _gsettings.connect("changed", set_terminal_args)
    set_terminal_args()
