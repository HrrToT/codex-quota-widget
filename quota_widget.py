"""Windows desktop quota widget. Tkinter and native RGBA surface; no inference requests."""
import argparse
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
import tkinter as tk
from tkinter import colorchooser, messagebox
import re

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tray_icon import TrayIcon
from screen_picker import ScreenPicker
from ring_surface import EdgeSnap, RingSurface, render_ring, FOREGROUND_ALPHA, LOGO_ALPHA, BACKGROUND_ALPHA
from desktop_support import (DEFAULT_COLORS, MODES, validated_colors, write_settings,
                             register_startup, codex_desktop_running, InstanceSignals)
FROZEN = getattr(sys, 'frozen', False)
DEFAULT_SETTINGS = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'CodexQuotaWidget/settings.json' if FROZEN else ROOT / 'settings.json'
SETTINGS = Path(os.environ.get('CODEX_QUOTA_SETTINGS', str(DEFAULT_SETTINGS)))
BG, PANEL, FG, MUTED, GREEN = '#111923', '#1b2735', '#f2f6fa', '#9bafc3', '#65e4b1'
BLUE = '#83bbff'


def find_codex():
    override = os.environ.get('CODEX_QUOTA_EXE')
    if override:
        if Path(override).is_file():
            return override
        raise RuntimeError('CODEX_QUOTA_EXE 指定的文件不存在。')
    direct = shutil.which('codex.exe')
    if direct:
        return direct
    bundled = Path(os.environ.get('LOCALAPPDATA', '')) / 'OpenAI/Codex/bin/codex.exe'
    if bundled.is_file():
        return str(bundled)
    npm = Path(os.environ.get('APPDATA', str(Path.home() / 'AppData/Roaming'))) / 'npm/node_modules/@openai'
    candidates = sorted(npm.glob('codex/node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe'))
    candidates += sorted(npm.glob('codex/vendor/*/bin/codex.exe'))
    if candidates:
        return str(candidates[0])
    raise RuntimeError('未找到 Codex CLI。请安装 CLI，或设置 CODEX_QUOTA_EXE。')


class QuotaReader:
    def __init__(self):
        self.process = None
        self.lock = threading.Lock()
        self.closed = False

    def close(self):
        with self.lock:
            self.closed = True
            p = self.process
        if p and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass

    def read(self, timeout=40):
        with self.lock:
            if self.closed:
                raise RuntimeError('组件已关闭。')
            p = subprocess.Popen(
                [find_codex(), 'app-server'], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self.process = p
        messages = queue.Queue()

        def receive():
            try:
                for line in p.stdout:
                    try:
                        messages.put(json.loads(line))
                    except ValueError:
                        continue
            finally:
                messages.put(None)

        receiver = threading.Thread(target=receive, daemon=True)
        receiver.start()
        deadline = time.monotonic() + timeout

        def send(message):
            p.stdin.write(json.dumps(message) + '\n')
            p.stdin.flush()

        def response(request_id):
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError('读取超时，请检查网络、代理和 Codex 登录状态。')
                try:
                    message = messages.get(timeout=remaining)
                except queue.Empty:
                    raise RuntimeError('读取超时，请检查网络、代理和 Codex 登录状态。') from None
                if message is None:
                    raise RuntimeError('Codex 读取进程提前退出，请检查 CLI 和登录状态。')
                if message.get('id') != request_id:
                    continue
                if 'error' in message:
                    code = message['error'].get('code', 'unknown')
                    raise RuntimeError(f'额度接口返回错误（{code}）。请在 Codex CLI 确认登录和网络状态。')
                return message.get('result', {})

        try:
            send({'id': 1, 'method': 'initialize', 'params': {
                'clientInfo': {'name': 'codex_quota_widget', 'title': 'Codex Quota Widget', 'version': '1.0.0'}
            }})
            response(1)
            send({'method': 'initialized'})
            send({'id': 2, 'method': 'account/rateLimits/read'})
            return response(2)
        finally:
            if p.poll() is None:
                p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=3)
            receiver.join(timeout=1)
            for stream in (p.stdin, p.stdout):
                try: stream.close()
                except OSError: pass
            with self.lock:
                if self.process is p:
                    self.process = None


def quota_rows(data):
    buckets = data.get('rateLimitsByLimitId')
    if not buckets:
        legacy = data.get('rateLimits')
        buckets = {legacy.get('limitId') or 'codex': legacy} if isinstance(legacy, dict) else {}
    rows = []
    for key, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        for slot in ('primary', 'secondary'):
            window = bucket.get(slot)
            if not isinstance(window, dict):
                continue
            used = window.get('usedPercent')
            valid = isinstance(used, (float, int)) and not isinstance(used, bool) and math.isfinite(used)
            remaining = max(0, min(100, 100 - used)) if valid else None
            mins = window.get('windowDurationMins')
            if isinstance(mins, (int, float)) and mins > 0:
                period = f'{mins / 1440:g} 天' if mins % 1440 == 0 else (f'{mins / 60:g} 小时' if mins % 60 == 0 else f'{mins:g} 分钟')
            else:
                period = '周期未知'
            rows.append({'label': str(bucket.get('limitName') or key), 'period': period,
                         'remaining': remaining, 'reset': window.get('resetsAt'), 'used': used,
                         'minutes': mins, 'bucket': key, 'slot': slot, 'plan': bucket.get('planType')})
    return rows


def quota_view(data):
    """Match actual durations, never infer primary=5h or combine unrelated buckets."""
    rows = quota_rows(data)
    buckets = data.get('rateLimitsByLimitId') or {}
    legacy = data.get('rateLimits') or {}
    bucket_id = 'codex' if 'codex' in buckets else next(iter(buckets), legacy.get('limitId') or 'codex')
    bucket = buckets.get(bucket_id, legacy)
    plan = bucket.get('planType') or ''
    main = [r for r in rows if r['bucket'] == bucket_id]
    windows = {}
    for row in main:
        key = {300: '5h', 10080: 'week'}.get(row['minutes'])
        if key:
            windows[key] = row
    dual = str(plan).lower() == 'plus' or ('5h' in windows and 'week' in windows)
    if dual:
        for key, mins in [('5h', 300), ('week', 10080)]:
            if key not in windows:
                windows[key] = {'label': str(bucket_id), 'period': '5 小时' if key == '5h' else '7 天',
                    'remaining': None, 'reset': None, 'used': None, 'minutes': mins,
                    'bucket': bucket_id, 'slot': 'missing', 'plan': plan}
        details = [windows['5h'], windows['week']]
        details += [r for r in rows if r not in details]
    else:
        details = rows
        if not windows and main:
            windows['quota'] = main[0]
    return {'plan': str(plan), 'dual': dual, 'windows': windows, 'details': details}


def window_key(row):
    return {300: '5h', 10080: 'week'}.get(row.get('minutes'), 'quota')


def period_color(key):
    return BLUE if key == '5h' else GREEN


def plan_label(plan):
    return {'plus': 'ChatGPT Plus', 'pro': 'ChatGPT Pro', 'free': 'ChatGPT Free'}.get(plan.lower(), plan or '当前账号')


def reset_text(timestamp):
    if not isinstance(timestamp, (int, float)):
        return '重置时间未提供'
    try:
        # User's Windows local timezone, explicitly displayed in the interface.
        stamp = datetime.fromtimestamp(timestamp).astimezone()
        return stamp.strftime('%m 月 %d 日  %H:%M 重置')
    except (ValueError, OverflowError, OSError):
        return '重置时间未提供'


class Widget:
    def __init__(self, root, smoke_path=None, show_surface=True):
        self.root, self.reader = root, QuotaReader()
        self.show_surface = show_surface
        self.results = queue.Queue()
        self.busy, self.last_success, self.closed = False, None, False
        self.latest_data, self.last_error = {}, False
        self.collapsed = False
        self.settings = {}
        try:
            self.settings = json.loads(SETTINGS.read_text(encoding='utf-8'))
            if not isinstance(self.settings, dict):
                self.settings = {}
        except (OSError, ValueError):
            pass
        self.colors = validated_colors(self.settings.get('colors'))
        self.startup_mode = self.settings.get('startup_mode', 'follow')
        if self.startup_mode not in MODES:
            self.startup_mode = 'manual'
        self.tray_hidden = False
        self.close_exits = bool(self.settings.get("close_exits", False))
        self.suspended = False
        self.generation = 0
        self.settings_panel = None
        self.pinned = bool(self.settings.get('topmost', True))
        self.selected_period = self.settings.get('selected_period', '5h')
        if self.selected_period not in ('5h', 'week'):
            self.selected_period = '5h'
        self.scale = max(1, root.winfo_fpixels('1i') / 96)
        self.width = round(336 * self.scale)
        self.ring_size = round(82 * self.scale)
        self.logo_path = ROOT / 'assets/openai-smooth.png'
        root.title('Codex 额度')
        root.configure(bg=BG)
        root.overrideredirect(True)
        root.attributes('-topmost', self.pinned)
        root.attributes('-toolwindow', True)
        root.geometry(f'{self.width}x{round(320 * self.scale)}')
        self.scroll = tk.Canvas(root, bg=BG, highlightthickness=0)
        self.scroll.pack(fill="both", expand=True)
        self.scroll.bind_all("<MouseWheel>", self.scroll_card)
        self.outer = tk.Frame(self.scroll, bg=BG, highlightbackground='#304257', highlightthickness=1)
        self.scroll.create_window(0, 0, window=self.outer, anchor='nw', width=self.width)
        header = tk.Frame(self.outer, bg=BG)
        header.pack(fill='x', padx=18, pady=(16, 8))
        title = tk.Label(header, text='●  CODEX', font=('Segoe UI', 12, 'bold'), fg=GREEN, bg=BG)
        title.pack(side='left')
        self.pin_button = self.button(header, '置顶', self.toggle_pin)
        self.pin_button.pack(side='right', padx=(8, 0))
        self.button(header, '×', self.request_close).pack(side='right')
        self.button(header, '收起', lambda: self.set_collapsed(True)).pack(side='right', padx=6)
        for area in (header, title):
            area.bind('<ButtonPress-1>', self.drag_start)
            area.bind('<B1-Motion>', self.drag_move)
            area.bind('<ButtonRelease-1>', lambda _: self.save())
        self.caption = tk.Label(self.outer, text='账号共享额度', fg=MUTED, bg=BG, font=('Microsoft YaHei UI', 9))
        self.caption.pack(anchor='w', padx=20)
        self.cards = tk.Frame(self.outer, bg=BG)
        self.cards.pack(fill='x', padx=18, pady=12)
        tk.Label(self.cards, text='正在读取额度…', fg=FG, bg=PANEL, padx=18, pady=24).pack(fill='x')
        self.status = tk.Label(self.outer, text='正在连接本机 Codex', fg=MUTED, bg=BG,
                               font=('Microsoft YaHei UI', 9), justify='left', wraplength=self.width - 40)
        self.status.pack(anchor='w', padx=20, pady=(0, 8))
        footer = tk.Frame(self.outer, bg=BG)
        footer.pack(fill='x', padx=18, pady=(0, 14))
        tk.Label(footer, text='每 5 分钟更新', fg=MUTED, bg=BG, font=('Microsoft YaHei UI', 8)).pack(side='left')
        self.refresh_button = self.button(footer, '刷新 ↻', self.refresh)
        self.refresh_button.pack(side='right')
        self.settings_button = self.button(footer, '设置 ▾', self.open_settings)
        self.settings_button.pack(side='right', padx=8)
        self.menu = tk.Menu(root, tearoff=False)
        self.menu.add_command(label='展开 / 收起', command=lambda: self.set_collapsed(not self.collapsed))
        self.menu.add_command(label='立即刷新', command=self.refresh)
        self.menu.add_command(label='切换置顶', command=self.toggle_pin)
        self.menu.add_command(label='颜色与启动设置', command=self.open_settings)
        self.menu.add_command(label='收起到系统托盘', command=self.hide_to_tray)
        self.menu.add_separator()
        self.menu.add_command(label='退出', command=self.close)
        root.bind('<Button-3>', lambda e: self.menu.tk_popup(e.x_root, e.y_root))
        root.bind('<Alt-F4>', lambda e: self.request_close())
        root.protocol('WM_DELETE_WINDOW', self.request_close)
        self.surface = RingSurface(root, self.on_surface_click, self.on_surface_moved,
                                   self.show_context_menu, self.request_close)
        self.snap = EdgeSnap(self.surface, bool(self.settings.get('edge_snap', True)))
        self.tray = TrayIcon(self.surface, self.restore_from_tray, self.show_tray_menu)
        self.surface.set_topmost(self.pinned)
        root.update_idletasks()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        try:
            x, y = int(self.settings.get('x', sw - self.width - 34)), int(self.settings.get('y', 80))
        except (TypeError, ValueError):
            x, y = sw - self.width - 34, 80
        root.geometry(f'+{max(0, min(x, sw-self.width))}+{max(0, min(y, sh-320))}')
        self.update_pin()
        self.resize()
        # The saved position is the compact ring's left edge.
        root.geometry(f'+{max(0, min(x + self.ring_size - self.width, sw-self.width))}+{max(0, min(y, sh-self.ring_size))}')
        self.set_collapsed(True, save=False)
        self.check_follow()
        root.after(100, self.refresh)
        root.after(100, self.poll)
        root.after(300000, self.auto_refresh)
        if smoke_path:
            root.after(12000, lambda: self.smoke_complete(smoke_path))

    def set_suspended(self, value):
        if value == self.suspended:
            return
        self.suspended = value
        if value:
            self.save()
            self.generation += 1
            self.reader.close()
            self.busy = False
            self.root.withdraw()
            self.surface.hide()
        else:
            self.reader = QuotaReader()
            if self.collapsed:
                if self.show_surface and not self.tray_hidden:
                    self.surface.show()
            elif not self.tray_hidden:
                self.root.deiconify()
            self.refresh()

    def check_follow(self):
        if self.closed:
            return
        if self.startup_mode == 'follow':
            try:
                self.set_suspended(not codex_desktop_running())
            except OSError:
                pass  # Keep current visibility if a process snapshot fails.
        else:
            self.set_suspended(False)
        self.root.after(3000, self.check_follow)

    def apply_preferences(self, colors, mode, edge_snap=None, close_exits=None):
        if mode not in MODES or any(not re.fullmatch(r'#[0-9a-fA-F]{6}', str(colors.get(k, ''))) for k in DEFAULT_COLORS):
            raise ValueError('请输入 #RRGGBB 格式的颜色，例如 #83bbff。')
        # Register before persisting; an OS error must not look like a saved startup mode.
        register_startup(mode)
        self.colors = validated_colors(colors)
        self.startup_mode = mode
        if close_exits is not None:
            self.close_exits = bool(close_exits)
        if edge_snap is not None:
            self.snap.set_enabled(edge_snap)
        self.save(strict=True)
        self.render(self.latest_data)
        self.resize()
        if mode != 'follow':
            self.set_suspended(False)
        else:
            self.set_suspended(not codex_desktop_running())

    def open_settings(self):
        self.tray_hidden = False
        if self.collapsed:
            self.set_collapsed(False)
        self.root.deiconify()
        if self.settings_panel:
            self.close_settings()
            return
        body = self.settings_panel = tk.Frame(self.outer, bg=BG, padx=18, pady=8)
        body.pack(fill='x')
        win = self.root
        self.settings_button.config(text='设置 ▴')
        def label(text):
            tk.Label(body, text=text, bg=BG, fg=FG, font=('Microsoft YaHei UI', 10), anchor='w').pack(fill='x', pady=(8, 8))
        label('额度颜色 · 圆环、数字与详情同步')
        values = {}
        for key, name in [('5h', '5 小时'), ('week', '周额度')]:
            row = tk.Frame(body, bg=BG)
            row.pack(fill='x', pady=6)
            tk.Label(row, text=name, width=8, anchor='w', bg=BG, fg=FG).pack(side='left')
            var = values[key] = tk.StringVar(value=self.colors[key])
            tk.Entry(row, textvariable=var, width=12, bg=PANEL, fg=FG, insertbackground=FG).pack(side='left', padx=10)
            def choose(v=var):
                initial = v.get() if re.fullmatch(r'#[0-9a-fA-F]{6}', v.get()) else '#ffffff'
                selected = colorchooser.askcolor(initialcolor=initial, parent=win, title='选择额度颜色')[1]
                if selected:
                    v.set(selected)
            self.button(row, '选色', choose).pack(side='left')
            self.button(row, '屏幕取色', lambda v=var: self.pick_screen_color(v)).pack(side='left', padx=4)
        self.button(body, '恢复默认颜色', lambda: [values[k].set(v) for k, v in DEFAULT_COLORS.items()]).pack(anchor='w', pady=6)
        label('边缘吸附')
        snap = tk.BooleanVar(value=self.snap.enabled)
        tk.Checkbutton(body, text='贴边收起一半，鼠标靠近时完整展开', variable=snap,
                       bg=BG, fg=FG, selectcolor=PANEL, activebackground=BG, activeforeground=FG).pack(anchor='w', pady=4)
        close_exits = tk.BooleanVar(value=self.close_exits)
        tk.Checkbutton(body, text='点击叉号直接退出（关闭则收起到托盘）', variable=close_exits,
                       bg=BG, fg=FG, selectcolor=PANEL, activebackground=BG, activeforeground=FG).pack(anchor='w', pady=5)
        label('启动方式')
        mode = tk.StringVar(value=self.startup_mode)
        for value, title in [('manual', '手动启动'), ('login', '登录 Windows 时启动'), ('follow', '随 Codex 启动和退出')]:
            tk.Radiobutton(body, text=title, variable=mode, value=value, bg=BG, fg=FG,
                           selectcolor=PANEL, activebackground=BG, activeforeground=FG).pack(anchor='w', pady=3)
        tk.Label(body, text='Codex 关闭后停止读取，后台监测下次启动。',
                 bg=BG, fg=MUTED, justify='left', font=('Microsoft YaHei UI', 9)).pack(anchor='w', pady=12)
        actions = tk.Frame(body, bg=BG)
        actions.pack(fill='x', pady=(8, 0))
        def apply():
            try:
                self.apply_preferences({k: v.get().strip() for k, v in values.items()}, mode.get(), snap.get(), close_exits.get())
            except (OSError, ValueError) as error:
                messagebox.showerror('未能保存设置', str(error), parent=win)
                return
            self.close_settings()
        self.button(actions, '保存', apply).pack(side='right', padx=10)
        self.button(actions, '取消', self.close_settings).pack(side='right')
        self.resize()
        self.scroll.yview_moveto(1)

    def close_settings(self):
        if self.settings_panel:
            self.settings_panel.destroy()
            self.settings_panel = None
        self.settings_button.config(text='设置 ▾')
        self.resize()
        self.scroll.yview_moveto(0)

    def scroll_card(self, event):
        if event.widget.winfo_toplevel() == self.root and self.outer.winfo_reqheight() > self.root.winfo_height():
            self.scroll.yview_scroll(int(-event.delta / 120) * 3, 'units')

    def pick_screen_color(self, variable):
        self.root.withdraw()
        self.surface.hide()
        def done(color):
            if color: variable.set(color)
            self.root.deiconify()
            self.root.lift()
            self.picker = None
        def capture():
            try:
                self.picker = ScreenPicker(self.root, done)
            except Exception as error:
                done(None)
                messagebox.showerror('屏幕取色失败', str(error), parent=self.root)
        self.root.after(180, capture)

    def request_close(self):
        if self.close_exits:
            self.close()
        else:
            self.hide_to_tray()

    def hide_to_tray(self):
        if not self.tray.added:
            self.tray.update(quota_view(self.latest_data), self.colors, force=True)
        if not self.tray.added:
            messagebox.showerror('无法收起', '系统托盘图标尚不可用，请稍后重试。', parent=self.root)
            return
        self.save()
        self.tray_hidden = True
        self.root.withdraw()
        self.surface.hide()

    def restore_from_tray(self):
        self.tray_hidden = False
        if self.collapsed:
            self.surface.show()
            self.snap.attach()
        else:
            self.root.deiconify()
            self.root.lift()

    def show_tray_menu(self, x, y):
        if getattr(self, "tray_menu", None):
            self.tray_menu.destroy()
        menu = self.tray_menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label='显示额度组件', command=self.restore_from_tray)
        menu.add_command(label='设置', command=self.open_settings)
        menu.add_command(label='刷新额度', command=self.refresh)
        menu.add_separator()
        menu.add_command(label='退出程序', command=self.close)
        self.surface.user.SetForegroundWindow(self.surface.hwnd)
        try: menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def button(self, parent, text, command):
        return tk.Button(parent, text=text, command=command, bg=BG, fg=MUTED, activebackground=PANEL,
                         activeforeground=FG, bd=0, cursor='hand2', font=('Microsoft YaHei UI', 9), padx=4)

    def update_pin(self):
        self.pin_button.config(text='已置顶' if self.pinned else '置顶', fg=GREEN if self.pinned else MUTED)

    def toggle_pin(self):
        self.pinned = not self.pinned
        self.root.attributes('-topmost', self.pinned)
        self.surface.set_topmost(self.pinned)
        self.update_pin()
        self.save()

    def drag_start(self, event):
        self.offset = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def drag_move(self, event):
        x, y = event.x_root - self.offset[0], event.y_root - self.offset[1]
        self.root.geometry(f'{x:+d}{y:+d}')
        self.root.update_idletasks()

    def on_surface_moved(self):
        self.snap.attach()
        self.save()

    def on_surface_click(self, x, y):
        mode = self.mode_at(x, y)
        if mode:
            self.select_period(mode)
        else:
            self.set_collapsed(False)

    def show_context_menu(self, x, y):
        try:
            self.menu.tk_popup(x, y)
        finally:
            self.menu.grab_release()

    def mode_at(self, x, y):
        if not quota_view(self.latest_data)['dual']:
            return None
        x, y = x / self.scale, y / self.scale
        if 49 <= y <= 66 and 20 <= x <= 62:
            return 'week' if self.selected_period == '5h' else '5h'
        return None

    def select_period(self, key):
        if key not in ('5h', 'week'):
            return
        self.selected_period = key
        self.draw_ring()
        self.save()

    def set_collapsed(self, collapsed, save=True):
        if collapsed == self.collapsed:
            return
        self.root.update_idletasks()
        if self.collapsed:
            right, y = self.surface.x + self.ring_size, self.surface.y
        else:
            right, y = self.root.winfo_x() + self.root.winfo_width(), self.root.winfo_y()
        self.collapsed = collapsed
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        if collapsed:
            self.root.withdraw()
            self.surface.move(max(0,min(right-self.ring_size,sw-self.ring_size)),max(0,min(y,sh-self.ring_size)))
            self.draw_ring()
            if self.show_surface:
                self.surface.show()
            self.snap.attach()
        else:
            self.surface.hide()
            self.root.attributes('-alpha', 0.98)
            self.root.configure(bg=BG)
            self.resize()
            height=self.outer.winfo_reqheight()
            self.root.geometry(f'+{max(0,min(right-self.width,sw-self.width))}+{max(0,min(y,sh-height))}')
            self.root.deiconify()
        if save:
            self.save()

    def draw_ring(self):
        view = quota_view(self.latest_data)
        self.displayed_period = self.selected_period if view['dual'] else next(iter(view['windows']), 'quota')
        self.ring_image = render_ring(view, self.displayed_period, self.scale, self.logo_path,
                                     self.colors, self.last_error, self.busy)
        self.surface.update(self.ring_image)
        self.tray.update(view, self.colors)

    def save(self, strict=False):
        try:
            x = self.surface.x if self.collapsed else self.root.winfo_x() + self.width - self.ring_size
            y = self.surface.y if self.collapsed else self.root.winfo_y()
            if self.snap.enabled and self.snap.anchor and self.collapsed:
                x, y = self.snap.anchor
            self.settings.update({'close_exits': self.close_exits, 'edge_snap': self.snap.enabled, 'x': x, 'y': y, 'topmost': self.pinned,
                                  'selected_period': self.selected_period, 'colors': self.colors,
                                  'startup_mode': self.startup_mode})
            write_settings(SETTINGS, self.settings)
        except OSError:
            if strict:
                raise

    def resize(self):
        self.root.update_idletasks()
        if not self.collapsed:
            height = min(self.outer.winfo_reqheight(), self.root.winfo_screenheight()-60)
            self.scroll.configure(scrollregion=(0,0,self.width,self.outer.winfo_reqheight()))
            self.root.geometry(f'{self.width}x{height}')
            y = max(0, min(self.root.winfo_y(), self.root.winfo_screenheight()-height-30))
            self.root.geometry(f'+{self.root.winfo_x()}+{y}')

    def refresh(self):
        if self.busy or self.closed or self.suspended:
            return
        self.busy = True
        self.draw_ring()
        self.refresh_button.config(state='disabled')
        self.status.config(text='正在刷新…', fg=MUTED)
        self.resize()
        reader, results, generation = self.reader, self.results, self.generation
        def worker():
            try:
                results.put((generation, True, reader.read()))
            except Exception as error:
                message = str(error) if isinstance(error, RuntimeError) else '读取失败，请检查 Codex CLI 和网络连接。'
                results.put((generation, False, message))
        threading.Thread(target=worker, daemon=True).start()

    def render(self, data):
        self.latest_data = data
        self.draw_ring()
        view = quota_view(data)
        self.caption.config(text=f"{plan_label(view['plan'])} · 账号共享额度")
        for child in self.cards.winfo_children():
            child.destroy()
        rows = view['details']
        if not rows:
            tk.Label(self.cards, text='接口未提供额度窗口', bg=PANEL, fg=FG, pady=24).pack(fill='x')
        for row in rows:
            frame = tk.Frame(self.cards, bg=PANEL)
            frame.pack(fill='x', pady=(0, 8))
            key = window_key(row)
            heading = {'5h': '5 小时额度', 'week': '周额度'}.get(key, f"{row['label'].upper()} / {row['period']}")
            if row['bucket'] != next(iter(view['windows'].values()), {}).get('bucket', row['bucket']):
                heading = f"{row['label']} · {row['period']}"
            tk.Label(frame, text=heading, fg=self.colors.get(key, self.colors['week']), bg=PANEL,
                     font=('Microsoft YaHei UI', 9)).pack(anchor='w', padx=16, pady=(12, 0))
            remaining = row['remaining']
            color = self.colors.get(key, self.colors['week'])
            large = '—' if remaining is None else f'{remaining:g}%'
            tk.Label(frame, text=large, fg=color, bg=PANEL, font=('Segoe UI', 26 if view['dual'] else 34, 'bold')).pack(anchor='w', padx=14)
            tk.Label(frame, text='剩余额度' if remaining is not None else '额度暂不可用', fg=FG, bg=PANEL,
                     font=('Microsoft YaHei UI', 9)).pack(anchor='w', padx=16)
            bar_width = self.width - 72
            bar = tk.Canvas(frame, width=bar_width, height=5, bg=PANEL, highlightthickness=0)
            bar.pack(padx=16, pady=(14, 10))
            bar.create_rectangle(0, 0, bar_width, 5, fill='#354559', outline='')
            if remaining is not None:
                bar.create_rectangle(0, 0, bar_width * remaining / 100, 5, fill=color, outline='')
            tk.Label(frame, text=reset_text(row['reset']), fg=MUTED, bg=PANEL,
                     font=('Microsoft YaHei UI', 9)).pack(anchor='w', padx=16, pady=(0, 14))

    def poll(self):
        if self.closed:
            return
        try:
            generation, success, result = self.results.get_nowait()
        except queue.Empty:
            pass
        else:
            if generation != self.generation or self.suspended:
                self.root.after(100, self.poll)
                return
            self.busy = False
            self.refresh_button.config(state='normal')
            if success:
                self.last_error = False
                self.last_success = datetime.now()
                self.render(result)
                self.status.config(text=f'更新于 {self.last_success:%H:%M:%S}', fg=MUTED)
            else:
                self.last_error = True
                prefix = f'旧数据 · 上次成功 {self.last_success:%H:%M}\n' if self.last_success else ''
                if not self.last_success:
                    for child in self.cards.winfo_children():
                        child.destroy()
                    tk.Label(self.cards, text='暂时无法读取', fg=FG, bg=PANEL, pady=24).pack(fill='x')
                self.status.config(text=prefix + result, fg='#f6c46b')
            self.draw_ring()
            self.resize()
        self.root.after(100, self.poll)

    def auto_refresh(self):
        self.refresh()
        self.root.after(300000, self.auto_refresh)

    def smoke_complete(self, path):
        self.root.update_idletasks()
        Path(path).write_text(json.dumps({'live_read_ok': self.last_success is not None,
            'geometry': self.root.geometry(), 'status': self.status.cget('text'),
            'collapsed': self.collapsed, 'borderless': bool(self.root.overrideredirect()),
            'rendering': 'single HWND, per-pixel RGBA',
            'opacity': FOREGROUND_ALPHA / 255,
            'center_opacity': BACKGROUND_ALPHA / 255,
            'logo_opacity': LOGO_ALPHA / 255,
            'tray_added': self.tray.added, 'startup_mode': self.startup_mode,
            'edge_snap': self.snap.enabled, 'close_exits': self.close_exits,
            'card_count': len(self.cards.winfo_children())}, ensure_ascii=False), encoding='utf-8')
        self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.save()
        self.reader.close()
        for timer in self.root.tk.call("after", "info"):
            self.root.after_cancel(timer)
        self.tray.close()
        self.surface.destroy()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description='Codex 额度桌面组件')
    parser.add_argument('--background', action='store_true')
    parser.add_argument('--settings', action='store_true')
    parser.add_argument('--quit', action='store_true')
    parser.add_argument('--once', action='store_true', help='Read once and print JSON')
    parser.add_argument('--smoke-report', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.once:
        reader = QuotaReader()
        try:
            result = json.dumps(reader.read(), ensure_ascii=False, indent=2)
            if sys.stdout:
                print(result)
            else:
                messagebox.showinfo('Codex 额度', result)
        finally:
            reader.close()
        return
    identity = str(SETTINGS) if os.environ.get('CODEX_QUOTA_SETTINGS') else ('CodexQuotaWidgetDesktop' if FROZEN else str(ROOT))
    key = hashlib.sha256(identity.encode()).hexdigest()[:16]
    signals = InstanceSignals(key)
    if signals.existing or args.quit:
        if args.quit:
            signals.signal('stop')
        elif not args.background:
            signals.signal('show')
        signals.close()
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (OSError, AttributeError):
        pass
    root = tk.Tk()
    app = Widget(root, args.smoke_report)
    try:
        if not args.smoke_report:
            register_startup(app.startup_mode)
    except (OSError, ValueError) as error:
        app.status.config(text=f'自动启动设置失败：{error}', fg='#f6c46b')
    def poll_signals():
        action = signals.poll()
        if action == 'stop':
            app.close()
            return
        if action == 'show':
            app.open_settings()
        if not app.closed:
            root.after(250, poll_signals)
    root.after(250, poll_signals)
    if args.settings or (app.suspended and not args.background):
        root.after(100, app.open_settings)
    try:
        root.mainloop()
    finally:
        signals.close()


if __name__ == '__main__':
    main()
