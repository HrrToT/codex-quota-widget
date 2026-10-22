"""One per-pixel-alpha Win32 surface. Dragging moves one HWND without repainting."""
import ctypes
from ctypes import wintypes as W
from functools import lru_cache
import os
from collections import deque
from pathlib import Path
from PIL import Image, ImageChops, ImageDraw, ImageFont

FOREGROUND_ALPHA = 214  # 84%
LOGO_ALPHA = 153        # 60%
BACKGROUND_ALPHA = 153 # 60%


def rgba(hex_color, alpha):
    return tuple(int(hex_color[i:i+2], 16) for i in (1, 3, 5)) + (alpha,)


@lru_cache(maxsize=64)
def font(size):
    path = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Fonts' / 'segoeuib.ttf'
    return ImageFont.truetype(str(path), size) if path.exists() else ImageFont.load_default(size=size)


def render_ring(view, selected, scale, logo_path, colors, error=False, busy=False):
    width = round(82*scale)
    factor = width/82 * 4
    size = width*4
    image = Image.new('RGBA', (size, size))
    background = ImageDraw.Draw(image)
    background.ellipse((2*factor,2*factor,size-2*factor,size-2*factor), fill=(17,25,35,BACKGROUND_ALPHA))
    if logo_path and Path(logo_path).exists():
        logo = Image.open(logo_path).convert('RGBA')
        logo.thumbnail((round(44*factor),round(44*factor)), Image.Resampling.LANCZOS)
        logo.putalpha(logo.getchannel('A').point(lambda a: round(a*LOGO_ALPHA/255)))
        image.alpha_composite(logo, ((size-logo.width)//2,(size-logo.height)//2))
    front = Image.new('RGBA', image.size)
    draw = ImageDraw.Draw(front)
    windows = view['windows']
    def ring(key, inset):
        remaining = windows.get(key, {}).get('remaining')
        color = colors.get(key, colors['week'])
        if error:
            color = '#f6c46b'
        # Pillow draws the stroke inward. These 8px bands have touching edges.
        box = (inset*factor,inset*factor,size-inset*factor,size-inset*factor)
        thickness = round(8*factor)
        draw.ellipse(box, outline=rgba('#354353',FOREGROUND_ALPHA), width=thickness)
        if remaining is not None and remaining > 0:
            if remaining >= 100:
                draw.ellipse(box, outline=rgba(color,FOREGROUND_ALPHA), width=thickness)
            else:
                draw.arc(box, -90, -90+3.6*remaining, fill=rgba(color,FOREGROUND_ALPHA), width=thickness)
    if view['dual']:
        ring('week',2)
        ring('5h',10)
    else:
        ring(selected,2)
    remaining = windows.get(selected, {}).get('remaining')
    value = '—' if remaining is None else f'{remaining:g}%'
    color = rgba(colors.get(selected,colors['week']),FOREGROUND_ALPHA)
    draw.text((size/2,38*factor),value,font=font(round(17*96/72*factor)),anchor='mm',fill=color)
    label = selected if selected in ('5h','week') else 'quota'
    draw.text((size/2,57*factor),label,font=font(round(9*96/72*factor)),anchor='mm',fill=color)
    if error or busy:
        draw.text((size/2,20*factor),'!' if error else '·',font=font(round(8*96/72*factor)),anchor='mm',
                  fill=rgba('#f6c46b' if error else '#9bafc3',FOREGROUND_ALPHA))
    image.alpha_composite(front)
    return image.resize((width,width),Image.Resampling.LANCZOS)


class RingSurface:
    def __init__(self, root, click, moved, context, close):
        self.root, self.click, self.moved, self.context, self.close_callback = root, click, moved, context, close
        self.x, self.y, self.size = 0, 0, 164
        self.visible, self.dragging, self.drag_moved = False, False, False
        # Keep the GIL for Win32 calls that can synchronously re-enter Tk window procedures.
        self.user = ctypes.PyDLL('user32', use_last_error=True)
        self.pending = deque()
        self.alive = True
        root.after(15, self._poll_callbacks)
        self.gdi = ctypes.WinDLL('gdi32', use_last_error=True)
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        U,G,K = self.user,self.gdi,self.kernel
        self.PROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t,W.HWND,W.UINT,W.WPARAM,W.LPARAM)
        class WNDCLASS(ctypes.Structure):
            _fields_=[('style',W.UINT),('lpfnWndProc',self.PROC),('cbClsExtra',ctypes.c_int),('cbWndExtra',ctypes.c_int),
                ('hInstance',W.HINSTANCE),('hIcon',W.HICON),('hCursor',W.HANDLE),('hbrBackground',W.HBRUSH),
                ('lpszMenuName',W.LPCWSTR),('lpszClassName',W.LPCWSTR)]
        self.WNDCLASS = WNDCLASS
        K.GetModuleHandleW.argtypes=[W.LPCWSTR]; K.GetModuleHandleW.restype=W.HMODULE
        U.RegisterClassW.argtypes=[ctypes.POINTER(WNDCLASS)]
        U.CreateWindowExW.argtypes=[W.DWORD,W.LPCWSTR,W.LPCWSTR,W.DWORD,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,W.HWND,W.HMENU,W.HINSTANCE,ctypes.c_void_p]
        U.CreateWindowExW.restype=W.HWND
        U.DefWindowProcW.argtypes=[W.HWND,W.UINT,W.WPARAM,W.LPARAM]; U.DefWindowProcW.restype=ctypes.c_ssize_t
        U.LoadCursorW.argtypes=[W.HINSTANCE,ctypes.c_void_p]; U.LoadCursorW.restype=W.HANDLE
        U.SetWindowPos.argtypes=[W.HWND,W.HWND,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,W.UINT]
        U.ShowWindow.argtypes=[W.HWND,ctypes.c_int]
        U.DestroyWindow.argtypes=[W.HWND]
        U.UnregisterClassW.argtypes=[W.LPCWSTR,W.HINSTANCE]
        U.SetCapture.argtypes=[W.HWND]; U.SetCapture.restype=W.HWND
        U.GetCursorPos.argtypes=[ctypes.POINTER(W.POINT)]
        U.GetDC.argtypes=[W.HWND]; U.GetDC.restype=W.HDC
        U.ReleaseDC.argtypes=[W.HWND,W.HDC]
        U.SetForegroundWindow.argtypes=[W.HWND]
        G.CreateCompatibleDC.argtypes=[W.HDC]; G.CreateCompatibleDC.restype=W.HDC
        G.CreateDIBSection.argtypes=[W.HDC,ctypes.c_void_p,W.UINT,ctypes.POINTER(ctypes.c_void_p),W.HANDLE,W.DWORD]; G.CreateDIBSection.restype=W.HBITMAP
        G.SelectObject.argtypes=[W.HDC,W.HANDLE]; G.SelectObject.restype=W.HANDLE
        G.DeleteObject.argtypes=[W.HANDLE]; G.DeleteDC.argtypes=[W.HDC]
        self.instance=K.GetModuleHandleW(None)
        self.class_name=f'CodexQuotaRing_{os.getpid()}_{id(self)}'
        self._callback=self.PROC(self._message)
        cls=WNDCLASS(0,self._callback,0,0,self.instance,None,U.LoadCursorW(None,ctypes.c_void_p(32649)),None,None,self.class_name)
        if not U.RegisterClassW(ctypes.byref(cls)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.hwnd=U.CreateWindowExW(0x00080000|0x00000080,self.class_name,'Codex 额度圆环',0x80000000,
                                   0,0,self.size,self.size,None,None,self.instance,None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())

    def _message(self, hwnd, msg, wp, lp):
        hook = getattr(self, "message_hook", None)
        if hook and hook(msg, wp, lp):
            return 0
        if msg == 0x0014: # no background erase, all painting is atomic RGBA upload
            return 1
        if msg == 0x0201:
            p=W.POINT();self.user.GetCursorPos(ctypes.byref(p))
            self.dragging=True;self.drag_moved=False
            self.drag_origin=(p.x,p.y,self.x,self.y)
            self.user.SetCapture(hwnd)
            return 0
        if msg == 0x0200 and self.dragging:
            p=W.POINT();self.user.GetCursorPos(ctypes.byref(p))
            sx,sy,ox,oy=self.drag_origin
            if abs(p.x-sx)+abs(p.y-sy)>5*self.size/82:
                self.drag_moved=True
            if self.drag_moved:
                self.move(ox+p.x-sx,oy+p.y-sy)
            return 0
        if msg == 0x0202 and self.dragging:
            self.dragging=False;self.user.ReleaseCapture()
            if self.drag_moved:
                self.pending.append(self.moved)
            else:
                x,y=ctypes.c_short(lp&0xffff).value,ctypes.c_short((lp>>16)&0xffff).value
                self.pending.append(lambda:self.click(x,y))
            return 0
        if msg == 0x0205:
            p=W.POINT();self.user.GetCursorPos(ctypes.byref(p))
            self.user.SetForegroundWindow(hwnd)
            self.pending.append(lambda:self.context(p.x,p.y))
            return 0
        if msg == 0x0010:
            self.pending.append(self.close_callback)
            return 0
        return self.user.DefWindowProcW(hwnd,msg,wp,lp)

    def _poll_callbacks(self):
        while self.pending and self.alive:
            self.pending.popleft()()
        if self.alive:
            self.root.after(15, self._poll_callbacks)

    def move(self,x,y):
        self.x,self.y=int(x),int(y)
        self.user.SetWindowPos(self.hwnd,None,self.x,self.y,0,0,0x0001|0x0004|0x0010)

    def set_topmost(self,value):
        self.user.SetWindowPos(self.hwnd,W.HWND(-1 if value else -2),0,0,0,0,0x0001|0x0002|0x0010)

    def show(self):
        self.visible=True
        self.user.ShowWindow(self.hwnd,4)

    def hide(self):
        self.visible=False
        self.user.ShowWindow(self.hwnd,0)

    def update(self,image):
        self.size=image.width
        class Header(ctypes.Structure):
            _fields_=[('size',W.DWORD),('width',W.LONG),('height',W.LONG),('planes',W.WORD),('bits',W.WORD),
                ('compression',W.DWORD),('image_size',W.DWORD),('xppm',W.LONG),('yppm',W.LONG),('used',W.DWORD),('important',W.DWORD)]
        class Blend(ctypes.Structure):
            _fields_=[('op',ctypes.c_byte),('flags',ctypes.c_byte),('alpha',ctypes.c_ubyte),('format',ctypes.c_byte)]
        self.user.UpdateLayeredWindow.argtypes=[W.HWND,W.HDC,ctypes.POINTER(W.POINT),ctypes.POINTER(W.SIZE),W.HDC,
                                              ctypes.POINTER(W.POINT),W.DWORD,ctypes.POINTER(Blend),W.DWORD]
        r,g,b,a=image.convert('RGBA').split()
        premultiplied=Image.merge('RGBA',(ImageChops.multiply(r,a),ImageChops.multiply(g,a),ImageChops.multiply(b,a),a))
        data=premultiplied.tobytes('raw','BGRA')
        screen=self.user.GetDC(None);dc=self.gdi.CreateCompatibleDC(screen)
        header=Header(ctypes.sizeof(Header),image.width,-image.height,1,32,0,len(data),0,0,0,0)
        bits=ctypes.c_void_p()
        bitmap=self.gdi.CreateDIBSection(screen,ctypes.byref(header),0,ctypes.byref(bits),None,0)
        if not bitmap:
            self.gdi.DeleteDC(dc);self.user.ReleaseDC(None,screen)
            raise ctypes.WinError(ctypes.get_last_error())
        old=self.gdi.SelectObject(dc,bitmap)
        try:
            ctypes.memmove(bits,data,len(data))
            dst,src,sz=W.POINT(self.x,self.y),W.POINT(0,0),W.SIZE(image.width,image.height)
            blend=Blend(0,0,255,1)
            if not self.user.UpdateLayeredWindow(self.hwnd,screen,ctypes.byref(dst),ctypes.byref(sz),dc,
                                               ctypes.byref(src),0,ctypes.byref(blend),2):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.gdi.SelectObject(dc,old);self.gdi.DeleteObject(bitmap)
            self.gdi.DeleteDC(dc);self.user.ReleaseDC(None,screen)

    def destroy(self):
        self.alive = False
        self.user.DestroyWindow(self.hwnd)
        self.user.UnregisterClassW(self.class_name,self.instance)

class EdgeSnap:
    """Animate one HWND at the nearest monitor edge; never repaint during motion."""
    def __init__(self, surface, enabled=False):
        self.surface = surface
        self.enabled = enabled
        self.edge = None
        self.bounds = None
        self.anchor = None
        self.target = None
        self.leave_at = None
        surface.user.MonitorFromWindow.argtypes = [W.HWND, W.DWORD]
        surface.user.MonitorFromWindow.restype = W.HANDLE
        surface.user.GetMonitorInfoW.argtypes = [W.HANDLE, ctypes.c_void_p]
        surface.root.after(30, self.tick)

    def monitor_bounds(self):
        class MonitorInfo(ctypes.Structure):
            _fields_ = [('size', W.DWORD), ('monitor', W.RECT), ('work', W.RECT), ('flags', W.DWORD)]
        info = MonitorInfo()
        info.size = ctypes.sizeof(info)
        monitor = self.surface.user.MonitorFromWindow(self.surface.hwnd, 2)
        if not self.surface.user.GetMonitorInfoW(monitor, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        r = info.monitor
        return r.left, r.top, r.right, r.bottom

    def attach(self):
        if not self.enabled:
            return
        s = self.surface
        left, top, right, bottom = self.bounds = self.monitor_bounds()
        size = s.size
        x, y = max(left, min(s.x, right-size)), max(top, min(s.y, bottom-size))
        distances = {'left': abs(x-left), 'right': abs(right-x-size),
                     'top': abs(y-top), 'bottom': abs(bottom-y-size)}
        self.edge = min(distances, key=distances.get)
        if self.edge == 'left': x = left
        elif self.edge == 'right': x = right-size
        elif self.edge == 'top': y = top
        else: y = bottom-size
        self.anchor = (x, y)
        self.target = self.anchor
        self.leave_at = None

    def set_enabled(self, enabled):
        if not enabled and self.anchor:
            self.surface.move(*self.anchor)
        self.enabled = bool(enabled)
        self.target = None
        self.anchor = None
        if enabled:
            self.attach()

    def folded_position(self):
        x, y = self.anchor
        half = self.surface.size // 2
        if self.edge == 'left': x -= half
        elif self.edge == 'right': x += half
        elif self.edge == 'top': y -= half
        else: y += half
        return x, y

    def tick(self):
        import time
        s = self.surface
        if not s.alive:
            return
        if self.enabled and s.visible and self.anchor and not s.dragging:
            p = W.POINT()
            s.user.GetCursorPos(ctypes.byref(p))
            x, y = self.anchor
            # Use the full-circle footprint plus a small margin, so approaching
            # the visible half reveals it before the pointer reaches the ring.
            near = x-16 <= p.x <= x+s.size+16 and y-16 <= p.y <= y+s.size+16
            if near:
                self.target = self.anchor
                self.leave_at = None
            else:
                if self.leave_at is None: self.leave_at = time.monotonic()
                if time.monotonic()-self.leave_at >= 0.65:
                    self.target = self.folded_position()
            if self.target:
                tx, ty = self.target
                dx, dy = tx-s.x, ty-s.y
                def step(d):
                    return d if abs(d) <= 2 else int(d*0.35) or (1 if d>0 else -1)
                if dx or dy: s.move(s.x+step(dx), s.y+step(dy))
        s.root.after(16, self.tick)
