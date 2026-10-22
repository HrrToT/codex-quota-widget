"""Windows integration. No elevated privileges or third-party runtime packages."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import winreg

RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
RUN_NAME = 'CodexQuotaWidget'
MODES = ('manual', 'login', 'follow')
DEFAULT_COLORS = {'5h': '#83bbff', 'week': '#65e4b1'}


def validated_colors(value):
    value = value if isinstance(value, dict) else {}
    return {key: (str(value.get(key)).lower() if re.fullmatch(r'#[0-9a-fA-F]{6}', str(value.get(key, ''))) else default)
            for key, default in DEFAULT_COLORS.items()}


def write_settings(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


def self_command(*args):
    if getattr(sys, 'frozen', False):
        return [sys.executable, *args]
    python = Path(sys.executable)
    if python.name.lower() == 'python.exe' and python.with_name('pythonw.exe').exists():
        python = python.with_name('pythonw.exe')
    return [str(python), str(Path(__file__).with_name('quota_widget.py')), *args]


def register_startup(mode):
    if mode not in MODES:
        raise ValueError('无效启动方式')
    if mode == 'manual':
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                try:
                    winreg.DeleteValue(key, RUN_NAME)
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            pass
    else:
        command = subprocess.list2cmdline(self_command('--background'))
        if len(command) > 260:
            raise ValueError('启动路径过长，请将程序安装在默认目录。')
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, command)


def is_desktop_path(path):
    """Desktop app only: never match the app-server or the npm Codex CLI."""
    path = str(path).replace('/', '\\').lower()
    name = path.rsplit('\\', 1)[-1]
    if name not in ('chatgpt.exe', 'codex.exe'):
        return False
    if '\\windowsapps\\openai.codex_' in path:
        return True
    # Unpackaged desktop installs have an Electron app.asar next to the executable.
    return name == 'codex.exe' and '\\bin\\' not in path and Path(path).parent.joinpath('resources', 'app.asar').is_file()


def codex_desktop_running():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    class ProcessEntry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
            ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
            ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
            ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
            ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), '无法枚举进程')
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() in ('chatgpt.exe', 'codex.exe'):
                handle = kernel.OpenProcess(0x1000, False, entry.th32ProcessID)
                if handle:
                    try:
                        name = ctypes.create_unicode_buffer(32768)
                        size = wintypes.DWORD(len(name))
                        if kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)) and is_desktop_path(name.value):
                            return True
                    finally:
                        kernel.CloseHandle(handle)
            ok = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        return False
    finally:
        kernel.CloseHandle(snapshot)


class InstanceSignals:
    """Named events allow a second launch/settings shortcut and clean uninstall."""
    def __init__(self, identity):
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        k = self.kernel
        k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        k.CreateMutexW.restype = wintypes.HANDLE
        k.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        k.CreateEventW.restype = wintypes.HANDLE
        k.SetEvent.argtypes = [wintypes.HANDLE]
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        self.mutex = k.CreateMutexW(None, False, 'Local\\CodexQuotaWidget_' + identity)
        self.existing = ctypes.get_last_error() == 183
        self.show = k.CreateEventW(None, False, False, 'Local\\CodexQuotaShow_' + identity)
        self.stop = k.CreateEventW(None, False, False, 'Local\\CodexQuotaStop_' + identity)

    def signal(self, action):
        self.kernel.SetEvent(self.stop if action == 'stop' else self.show)

    def poll(self):
        if self.kernel.WaitForSingleObject(self.stop, 0) == 0:
            return 'stop'
        if self.kernel.WaitForSingleObject(self.show, 0) == 0:
            return 'show'
        return None

    def close(self):
        for handle in (self.show, self.stop, self.mutex):
            if handle:
                self.kernel.CloseHandle(handle)
