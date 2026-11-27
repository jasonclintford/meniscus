"""Windows stand-ins for the Linux desktop tools Meniscus uses on Linux.

  clipboard text      xclip / wl-paste      ->  Win32 clipboard (CF_UNICODETEXT)
  copy file           xclip text/uri-list   ->  Win32 clipboard (CF_HDROP), as Explorer does
  trash               gio trash             ->  SHFileOperationW with FOF_ALLOWUNDO (Recycle Bin)
  show in folder      FileManager1 / xdg    ->  explorer /select,
  open                xdg-open              ->  os.startfile
  cancel a job        killpg                ->  taskkill /T (the whole process tree)

Importing this module also stops child processes (ffmpeg, gallery-dl, yt-dlp) from opening
console windows when Meniscus runs as a windowed app.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import threading
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

CF_UNICODETEXT = 13
CF_HDROP = 15
GMEM_MOVEABLE = 0x0002
HWND_MESSAGE = wintypes.HWND(-3)

user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
user32.RegisterClipboardFormatW.restype = wintypes.UINT
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = wintypes.LPVOID
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]

# ------------------------------------------------------- no console windows ----
_Popen = subprocess.Popen


class _QuietPopen(_Popen):
    def __init__(self, *args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
        super().__init__(*args, **kwargs)


subprocess.Popen = _QuietPopen  # subprocess.run() looks Popen up on the module, so it is covered too

# --------------------------------------------------------------- clipboard ----
_clip_lock = threading.Lock()


def _open_clipboard(hwnd=None) -> bool:
    for _ in range(10):  # another app may hold it for a moment
        if user32.OpenClipboard(hwnd):
            return True
        time.sleep(0.03)
    return False


_last_seq = [None, ""]


def read_clipboard() -> str:
    """Clipboard text, re-read only when the clipboard has actually changed."""
    seq = user32.GetClipboardSequenceNumber()
    if seq == _last_seq[0]:
        return _last_seq[1]
    text = ""
    with _clip_lock:
        if user32.IsClipboardFormatAvailable(CF_UNICODETEXT) and _open_clipboard():
            try:
                h = user32.GetClipboardData(CF_UNICODETEXT)
                p = kernel32.GlobalLock(h) if h else None
                if p:
                    try:
                        text = ctypes.wstring_at(p)[:20000]
                    finally:
                        kernel32.GlobalUnlock(h)
            finally:
                user32.CloseClipboard()
    _last_seq[0], _last_seq[1] = seq, text
    return text


def _global(data: bytes):
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not h:
        raise OSError("GlobalAlloc failed")
    p = kernel32.GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    kernel32.GlobalUnlock(h)
    return h


def copy_file_to_clipboard(path: str) -> bool:
    """Put the file on the clipboard the way Explorer's Copy does, so it pastes into Discord."""
    # DROPFILES header: offset to the file list, a POINT, fNC, fWide; then a double-NUL-ended list.
    files = (os.path.abspath(path) + "\0\0").encode("utf-16-le")
    header = (20).to_bytes(4, "little") + bytes(12) + (1).to_bytes(4, "little")
    # SetClipboardData fails after EmptyClipboard unless the clipboard has an owner window.
    hwnd = user32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, HWND_MESSAGE, None, None, None)
    with _clip_lock:
        if not _open_clipboard(hwnd):
            if hwnd:
                user32.DestroyWindow(hwnd)
            return False
        try:
            user32.EmptyClipboard()
            h = _global(header + files)
            if not user32.SetClipboardData(CF_HDROP, h):
                kernel32.GlobalFree(h)
                return False
            effect = user32.RegisterClipboardFormatW("Preferred DropEffect")
            h2 = _global((1).to_bytes(4, "little"))  # DROPEFFECT_COPY
            if not user32.SetClipboardData(effect, h2):
                kernel32.GlobalFree(h2)
            return True
        finally:
            user32.CloseClipboard()
            if hwnd:
                user32.DestroyWindow(hwnd)


# ------------------------------------------------------------------- trash ----
class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", wintypes.LPVOID), ("lpszProgressTitle", wintypes.LPCWSTR)]


shell32.SHFileOperationW.argtypes = [ctypes.POINTER(_SHFILEOPSTRUCTW)]
shell32.SHFileOperationW.restype = ctypes.c_int

FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400
FOF_WANTNUKEWARNING = 0x4000  # ask before deleting outright where there is no Recycle Bin


def trash(path: str) -> str:
    """Move a file to the Recycle Bin. Returns an error message, or '' on success."""
    src = ctypes.create_unicode_buffer(os.path.abspath(path) + "\0")  # list must end in two NULs
    op = _SHFILEOPSTRUCTW(wFunc=FO_DELETE, pFrom=ctypes.cast(src, wintypes.LPCWSTR),
                          fFlags=FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_WANTNUKEWARNING)
    rc = shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0 or op.fAnyOperationsAborted:
        return f"could not move to the Recycle Bin (code {rc:#x})" if rc else "cancelled"
    return ""


# ------------------------------------------------------------ shell / procs ----
def reveal(path: str) -> None:
    """Open an Explorer window with the file selected."""
    if os.path.isdir(path):
        os.startfile(path)
    else:  # explorer wants /select,"path" as one unsplit token, so pass a command string
        subprocess.Popen(f'explorer.exe /select,"{os.path.abspath(path)}"')


def open_path(path: str) -> None:
    os.startfile(path)


def kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
