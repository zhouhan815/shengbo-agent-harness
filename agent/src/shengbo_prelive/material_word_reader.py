"""Isolated Windows Word reader for binary DOC. Never imported by the Agent."""
from __future__ import annotations

import json
from pathlib import Path
import sys


def _word_processes():
    """Native snapshot only; no shell and no access to document contents."""
    import ctypes
    from ctypes import wintypes
    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    found = set()
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == "winword.exe":
                found.add(entry.th32ProcessID)
            ok = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return found


def main():
    import pythoncom
    import win32com.client
    import win32process
    source, target, receipt = map(Path, sys.argv[1:4])
    app = document = None
    record = {"stage": "initializing"}
    pythoncom.CoInitialize()
    try:
        before = _word_processes()
        app = win32com.client.DispatchEx("Word.Application")
        new_processes = _word_processes() - before
        record.update(stage="configuring")
        if len(new_processes) == 1:
            record["owned_word_pid"] = next(iter(new_processes))
        receipt.write_text(json.dumps(record), encoding="utf-8")
        app.Visible = False
        app.DisplayAlerts = 0
        app.AutomationSecurity = 3  # msoAutomationSecurityForceDisable
        app.Options.UpdateLinksAtOpen = False
        app.Options.UpdateFieldsAtPrint = False
        app.Options.SaveNormalPrompt = False
        record["word_version"] = str(app.Version)
        # Unknown passwords are never interactively requested. Conversion is
        # confined to our temporary copy; nothing saves back to source.doc.
        record["stage"] = "opening_readonly"
        receipt.write_text(json.dumps(record), encoding="utf-8")
        document = app.Documents.Open(FileName=str(source), ConfirmConversions=False, ReadOnly=True,
                                      AddToRecentFiles=False, Revert=False, PasswordDocument="shengbo-unavailable-password",
                                      WritePasswordDocument="shengbo-unavailable-password", Visible=False,
                                      OpenAndRepair=False, NoEncodingDialog=True)
        # Word exposes Hwnd on its document Window, not Application.
        try:
            _, pid = win32process.GetWindowThreadProcessId(document.ActiveWindow.Hwnd)
            if pid not in before:
                record["owned_word_pid"] = pid
        except Exception:
            pass
        record["stage"] = "saving_derived_copy"
        receipt.write_text(json.dumps(record), encoding="utf-8")
        document.SaveAs2(FileName=str(target), FileFormat=16, AddToRecentFiles=False)
        record["stage"] = "converted"
        receipt.write_text(json.dumps(record), encoding="utf-8")
        return 0
    except Exception as exc:
        # Error category and COM HRESULT only, never source content or paths.
        record.update(error_type=type(exc).__name__, hresult=getattr(exc, "hresult", None))
        receipt.write_text(json.dumps(record), encoding="utf-8")
        return 2
    finally:
        if document is not None:
            try:
                document.Close(SaveChanges=0)
            except Exception:
                pass
        if app is not None:
            try:
                app.Quit(SaveChanges=0)
            except Exception:
                pass
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    raise SystemExit(main())
