"""User-interface text. The app is English-only; strings live here so wording stays in one place.
t(key) returns the text (or the key itself if it is missing); f(key, *args) formats it."""

_TEXT = {
    "tray.tooltip": "DisplayDesktop",
    "menu.overview": "Overview",
    "menu.prev": "Left",
    "menu.next": "Right",
    "menu.keys": "Ctrl+Alt+",
    "menu.load": "Load",
    "menu.save": "Save",
    "menu.settings": "Settings",
    "settings.title": "DisplayDesktop Settings",
    "settings.startup": "Start With Windows",
    "settings.startMenu": "Add To Start Menu",
    "settings.console": "Show Console",
    "settings.gpu": "Use GPU Acceleration",
    "settings.noPreviews": "Disable Overview Previews (Less Compute)",
    "settings.restoreApps": "Restore Applications On Restart",
    "settings.recovery": "Recovery",
    "settings.close": "Close",
    "settings.folder": "Settings Folder",
    "settings.apply": "Apply",
    "settings.default": "Default",
    "settings.restore": "Restore Hidden Windows",
    "settings.advanced": "Show Advanced",
    "settings.encrypt": "Encrypt Settings",
    "settings.rememberKey": "Remember Key",
    "settings.revert": "Revert",
    "key.unapplied": "The new encryption key was not applied.\n\nApply it, or revert to the saved key?",
    "key.unappliedNew": "The settings are not encrypted yet: the key was not applied.\n\n"
                        "Apply it to encrypt them, or revert to leave them as they are?",
    "settings.encrypted": "Status: Encrypted",
    "settings.notEncrypted": "Status: Not encrypted",
    "key.title": "Encryption Key",
    "key.ask": "The settings in this folder are encrypted:\n{0}\n\nEnter the key:",
    "key.label": "Encryption key",
    "key.try": "Try Key",
    "key.wrong": "That key doesn't open the settings.",
    "key.retry": "That key doesn't open the settings.\n\nTry again?",
    "key.show": "Show",
    "key.hide": "Hide",
    "msg.encryptFail": "Some files could not be rewritten (in use, or encrypted with another key):\n\n{0}",
    "recovery.title": "DisplayDesktop Recovery",
    "recovery.unreadable": "Some settings files can't be read (see below). Quit, recreate them (they are "
                           "deleted and start fresh), or choose another settings folder.",
    "badFolder.title": "Settings Folder Can't Be Used",
    "badFolder.text": "Some files in this folder can't be read, so it isn't used. Choose another folder.\n\n{0}",
    "msg.recoveryOpen": "Recovery is waiting for an answer: answer it first.",
    "recovery.none": "No problems found.",
    "recovery.some": "Some files can't be read right now (see below).",
    "recovery.folder": "Settings folder: {0}",
    "recovery.files": "Files:",
    "recovery.ok": "OK",
    "recovery.missing": "Not there yet (made when needed)",
    "recovery.locked": "Encrypted with a key not entered",
    "recovery.held": "In use by another program",
    "recovery.cantOpen": "Can't be opened: {0}",
    "recovery.offline": "Its folder can't be reached",
    "recovery.newer": "Its revision number is newer ({0}; this version reads up to {1}): "
                      "<a href=\"{2}\">check for updates</a>",
    "recovery.notJson": "Damaged: not valid at line {0}, column {1} ({2})",
    "recovery.notText": "Damaged: not a DisplayDesktop file",
    "recovery.tooDeep": "Damaged: nested too deeply",
    "recovery.damaged": "Damaged: {0}",
    "recovery.journal": "Crash record of hidden windows. {0}. Removed: use Restore Hidden Windows.",
    "recovery.journalUnread": "Crash record of hidden windows. Can't be opened for a while (in use, or its "
                              "folder offline): use Restore Hidden Windows.",
    "recovery.notJournal": "Damaged: not a list of windows",
    "recovery.badRecord": "Damaged: a window in it is wrong",
    "recovery.recreate": "Recreate Files",
    "recovery.clear": "Clear Desktops",
    "recovery.quit": "Quit",
    "recovery.continue": "Continue",
    "msg.saveLocked": "Desktops not saved: the saved desktops are encrypted with another key.",
    "msg.loadLocked": "Desktops not loaded: the saved desktops are encrypted with another key.",
    "restore.title": "Restore Hidden Windows",
    "restore.about": "Lists every hidden window. Tick the ones to bring back to desktop 1.",
    "restore.none": "No hidden windows found.",
    "restore.ok": "OK",
    "restore.cancel": "Cancel",
    "problem.unreachable": "The settings folder can't be found:\n{0}\n\nThe default folder is used for now. "
                           "Windows hidden before may still be hidden.",
    "problem.saveFailed": "DisplayDesktop can't save to its settings folder:\n{0}\n\n"
                          "If it stops now, hidden windows may stay hidden.",
    "problem.damaged": "The list of windows hidden in the last session can't be read. "
                       "Some windows may still be hidden.",
    "msg.moveConfirm": "Move saved settings?\n\nYour settings and saved desktops move to:\n{0}",
    "msg.defaultConfirm": "Use the default folder?\n\nYour settings and saved desktops move back to:\n{0}",
    "msg.useFolderData": "Use the settings in that folder?\n\n{0}\n\nYour settings and saved desktops are taken from there.",
    "msg.bothHaveData": "Both folders have settings. Keep the ones in this folder?\n\n{0}\n\n"
                        "Yes: keep that folder's settings.\nNo: keep your current settings (they replace those).\n"
                        "Cancel: change nothing.",
    "msg.moveFail": "The settings could not be moved.\n\n{0}",
    "menu.exit": "Exit",
    "msg.consoleFail": "The console could not be opened (python.exe was not found).",
    "msg.startupFail": "Start With Windows could not be changed.",
    "msg.hotkeyFail": "Some shortcuts could not be registered (another app may be using them): ",
    "msg.saveConfirm": "Save desktops?\n\nSaves where every window is. Replaces your last save.",
    "msg.saved": "Saved {0} windows and the desktop each monitor shows.",
    "msg.saveFailed": "Desktops not saved: the settings folder can't be written.",
    "msg.saveBusy": "Desktops not saved: the saved desktops are in use. Try again shortly.",
    "msg.loadBusy": "Desktops not loaded: the saved desktops are in use. Try again shortly.",
    "msg.loadFailed": "Desktops not loaded: the settings folder can't be reached.",
    "msg.saveUnreadable": "Desktops not saved: the saved desktops can't be read.",
    "msg.loadUnreadable": "Desktops not loaded: the saved desktops can't be read.",
    "msg.loadConfirm": "Load desktops?\n\nPuts every window back where you saved it. The current arrangement is not kept.",
    "msg.exitConfirm": "Are you sure you want to quit DisplayDesktop?",
    "msg.restoreConfirm": "Restore hidden windows?\n\nLists every hidden window, so you can bring the ones you pick "
                          "back to desktop 1.",
    "msg.recreateConfirm": "Recreate settings files?\n\nEach file is written again. Files that can't be read start "
                           "fresh. Can't be undone.",
    "msg.clearConfirm": "Clear desktops?\n\nMerges all desktops and deletes your save. Can't be undone.",
    "msg.alreadyRunning": "DisplayDesktop is already running.",
    "msg.crash": "DisplayDesktop ran into an unexpected error and will close. All hidden windows have been restored.\n\n{0}",
    "msg.startFailed": "DisplayDesktop could not start.\n\n{0}",
    "msg.errorWhere": "Where (last step first):",
    "msg.errorVersion": "DisplayDesktop {0}. Press Ctrl+C to copy this message.",
    "ov.title": "DisplayDesktop — Overview",
    "ov.legend": "click a window: go to it   •   drag a window onto any desktop card   •   drag a card title onto another card to swap them, or onto another monitor row   •   right-click a window: move menu   •   click a card: switch   •   Esc: close",
    "ov.monitor": "Monitor {0}",
    "ov.desktop": "⠿   Desktop {0}",
    "ov.active": "ACTIVE",
    "ov.empty": "Empty — drag windows here",
    "ov.more": "+{0} more",
    "ov.drop": "⬇  Drop here",
    "ov.swap": "⇄  Swap",
    "ov.menu.move": "Move to:  Monitor {0}  •  Desktop {1}",
    "ov.menu.activeSuffix": "  (active)",
    "ov.menu.goto": "Go to this window",
    "ov.tip.window": "Click: go to this window  •  Drag onto a desktop card  •  Right-click: move menu",
    "ov.tip.header": "Drag onto another desktop card to swap the two, or onto another monitor row to move the whole desktop",
    "ov.tip.add": "Create a new desktop on this monitor and switch to it",
    "ov.tip.trash": "Delete this desktop (its windows move to the desktop next to it)",
}


def t(key: str) -> str:
    return _TEXT.get(key, key)


def f(key: str, *args) -> str:
    return t(key).format(*args)


ERROR_STEPS = 4  # the last code steps an error message shows


def error_text(exc: BaseException) -> str:
    """An error for a message box: its type and text, then where it happened (the last steps:
    file, line, function), so a report says where to look. Ctrl+C in the box copies it all."""
    import traceback
    from . import __version__
    lines = [f"{type(exc).__name__}: {exc}"]
    steps = traceback.extract_tb(exc.__traceback__)[-ERROR_STEPS:]
    if steps:
        lines += ["", t("msg.errorWhere")]
        lines += [f"  {s.filename.replace(chr(92), '/').rsplit('/', 1)[-1]} line {s.lineno}, {s.name}"
                  for s in reversed(steps)]
    lines += ["", f("msg.errorVersion", __version__)]
    return "\n".join(lines)
