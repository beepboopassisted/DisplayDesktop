"""Recovery's report: what is wrong, for the one dialog every error, crash and unreadable file
leads to (restore_window.RecoveryWindow). Every data file of the settings folder is listed with its
state in words, so the user can decide: Restore Hidden Windows, Recreate Files (the files that can't
be read at all), Quit, or Continue. Also opened on request (`run recovery`, `--recovery`).
"""
from __future__ import annotations

import json
import os
from collections import namedtuple

from . import crypto, datafolder, paths
from . import strings as L

# One file: `shown` (its name, or its path when outside the folder in use), `state` in words, and
# `kind`: "ok", "missing", "unreadable" (can't be read at all: Recreate Files deletes it), "locked"
# (encrypted with a key not entered), "held" (can't be opened now), "journal" (a crash record a run
# left that could not be read: told, already removed)
Row = namedtuple("Row", "path shown state kind")
Report = namedtuple("Report", "summary rows unreadable locked problem")  # problem: anything wrong at all


def describe(exc: BaseException) -> tuple[str, str]:
    """(state in words, kind) of a read that failed (paths.read_json's errors)."""
    if isinstance(exc, FileNotFoundError):
        return L.t("recovery.missing"), "missing"
    if isinstance(exc, crypto.Locked):
        return L.t("recovery.locked"), "locked"
    if isinstance(exc, OSError):
        if isinstance(exc, PermissionError) or getattr(exc, "winerror", 0) in (32, 33):
            return L.t("recovery.held"), "held"
        return L.f("recovery.cantOpen", exc.strerror or exc), "held"
    if isinstance(exc, paths.NewerFormat):
        from .settings_window import REPO_URL  # ("check for updates" is a link to the project page)
        return L.f("recovery.newer", exc.fmt, paths.FORMAT, REPO_URL), "unreadable"
    if isinstance(exc, json.JSONDecodeError):
        return L.f("recovery.notJson", exc.lineno, exc.colno, exc.msg), "unreadable"
    if isinstance(exc, UnicodeDecodeError):
        return L.t("recovery.notText"), "unreadable"
    if isinstance(exc, RecursionError):
        return L.t("recovery.tooDeep"), "unreadable"
    return L.f("recovery.damaged", exc), "unreadable"


def check(path: str) -> tuple[str, str]:
    """(state in words, kind) of one data file, read the way the app reads it (no question asked)."""
    if paths.known_offline(os.path.dirname(path)) or paths.paused_as_gone(os.path.dirname(path)):
        return L.t("recovery.offline"), "held"  # (never a wait on the network timeout)
    try:
        paths.read_json(path, ask=False)
    except (OSError, ValueError, RecursionError) as exc:
        return describe(exc)
    return L.t("recovery.ok"), "ok"


def build(folder: str, problem: str = "", skip=(), journals: dict | None = None,
          others: tuple[str, ...] = ()) -> Report:
    """The report for `folder` (the settings folder in use): `problem` (the folder problem the tray
    reports, in words, or ""), `skip` (normcased paths never asked about
    again: shown, never counted as Recreate's), `journals` ({path: why} of crash records that could
    not be read), `others` (folders whose unreadable files are listed too: the default folder)."""
    rows: list[Row] = []
    for name in paths.DATA_FILES:
        p = os.path.join(folder, name)
        state, kind = check(p)
        if kind == "unreadable" and name == paths.JOURNAL_FILE:
            kind = "journal"  # (never Recreate's: the manager tells and removes it)
        rows.append(Row(p, name, state, kind))
    for other in others:
        for name in paths.ASKED_FILES:
            p = os.path.join(other, name)
            state, kind = check(p)
            if kind == "unreadable":
                rows.append(Row(p, datafolder.shown(p), state, kind))
    for p, why in (journals or {}).items():
        if why == "unread":
            state = L.t("recovery.journalUnread")
        else:
            state = L.f("recovery.journal", describe(why)[0] if isinstance(why, BaseException)
                        else L.t(f"recovery.{why}"))
        row = Row(p, os.path.basename(p) if datafolder.same(os.path.dirname(p), folder)
                  else datafolder.shown(p), state, "journal")
        same = [i for i, r in enumerate(rows) if datafolder.same(r.path, p)]
        if not same:
            rows.append(row)
        elif rows[same[0]].kind in ("missing", "held"):
            rows[same[0]] = row  # (removed after it was found damaged, or still unopened: why it matters)
    unreadable = [r.path for r in rows if r.kind == "unreadable" and os.path.normcase(r.path) not in skip]
    summary = [problem] if problem else []
    wrong = bool(problem) or any(r.kind not in ("ok", "missing") for r in rows)
    if unreadable:
        summary.append(L.t("recovery.unreadable"))  # (the project page is a link on every Recovery page)
    if any(r.kind == "journal" for r in rows) and L.t("problem.damaged") not in summary:
        summary.append(L.t("problem.damaged"))
    if not summary:
        summary.append(L.t("recovery.none" if not wrong else "recovery.some"))
    summary.append(L.f("recovery.folder", datafolder.shown(folder)))
    return Report("\n\n".join(summary), rows, unreadable, any(r.kind == "locked" for r in rows), wrong)
