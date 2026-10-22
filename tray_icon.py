"""Small-size quota rings and a native Windows notification-area icon."""
import ctypes
from ctypes import wintypes as W
from PIL import Image, ImageDraw, ImageChops


def render_tray(view, colors, size=24):
    k=4
    image=Image.new('RGBA',(size*k,size*k))
    d=ImageDraw.Draw(image)
    # Designed directly at tray scale: 2px bands at 16px, separated by 1px.
    unit=size/16*k
    def ring(key,inset,width):
        box=(inset*unit,inset*unit,size*k-inset*unit-1,size*k-inset*unit-1)
        d.ellipse(box,outline='#536274',width=round(width*unit))
        value=view['windows'].get(key,{}).get('remaining')
        if value is not None and value>0:
            color=colors.get(key,colors['week'])
            if value>=100:d.ellipse(box,outline=color,width=round(width*unit))
            else:d.arc(box,-90,-90+value*3.6,fill=color,width=round(width*unit))
    if view['dual']:
        ring('week',0.5,2)
        ring('5h',3.5,2)
    else:ring(next(iter(view['windows']),'week'),1,3)
    image = image.resize((size,size),Image.Resampling.LANCZOS)
    # Remove resampling ringing at the tiny center aperture.
    radius = max(0.5, size / 32)
    ImageDraw.Draw(image).ellipse((size/2-radius,size/2-radius,size/2+radius,size/2+radius),fill=(0,0,0,0))
    return image


class TrayIcon:
    MESSAGE=0x8000+20
    def __init__(self,surface,activate,menu):
        self.surface=surface;self.activate=activate;self.menu=menu
        self.user=surface.user;self.gdi=surface.gdi
        self.shell=ctypes.WinDLL('shell32',use_last_error=True)
        class GUID(ctypes.Structure):
            _fields_=[('a',W.DWORD),('b',W.WORD),('c',W.WORD),('d',ctypes.c_ubyte*8)]
        class Notify(ctypes.Structure):
            _fields_=[('cbSize',W.DWORD),('hWnd',W.HWND),('uID',W.UINT),('uFlags',W.UINT),
                ('uCallbackMessage',W.UINT),('hIcon',W.HICON),('szTip',W.WCHAR*128),('dwState',W.DWORD),
                ('dwStateMask',W.DWORD),('szInfo',W.WCHAR*256),('uTimeoutOrVersion',W.UINT),
                ('szInfoTitle',W.WCHAR*64),('dwInfoFlags',W.DWORD),('guidItem',GUID),('hBalloonIcon',W.HICON)]
        self.Notify=Notify
        self.shell.Shell_NotifyIconW.argtypes=[W.DWORD,ctypes.POINTER(Notify)]
        self.user.RegisterWindowMessageW.argtypes=[W.LPCWSTR]
        self.taskbar=self.user.RegisterWindowMessageW('TaskbarCreated')
        self.user.DestroyIcon.argtypes=[W.HICON]
        self.icon=None;self.added=False;self.last=None
        surface.message_hook=self.message

    def message(self,msg,wp,lp):
        if msg==self.taskbar:
            self.added=False
            if self.last:self.surface.pending.append(lambda:self.update(*self.last,force=True))
            return True
        if msg==self.MESSAGE:
            if lp in (0x0202,0x0203):self.surface.pending.append(self.activate)
            elif lp==0x0205:
                p=W.POINT();self.user.GetCursorPos(ctypes.byref(p))
                self.surface.pending.append(lambda:self.menu(p.x,p.y))
            return True
        return False

    def make_icon(self,image):
        class Header(ctypes.Structure):
            _fields_=[('size',W.DWORD),('width',W.LONG),('height',W.LONG),('planes',W.WORD),('bits',W.WORD),
                ('compression',W.DWORD),('image_size',W.DWORD),('xppm',W.LONG),('yppm',W.LONG),('used',W.DWORD),('important',W.DWORD)]
        class IconInfo(ctypes.Structure):
            _fields_=[('fIcon',W.BOOL),('x',W.DWORD),('y',W.DWORD),('mask',W.HBITMAP),('color',W.HBITMAP)]
        self.user.CreateIconIndirect.argtypes=[ctypes.POINTER(IconInfo)]
        self.user.CreateIconIndirect.restype=W.HICON
        self.gdi.CreateBitmap.argtypes=[ctypes.c_int,ctypes.c_int,W.UINT,W.UINT,ctypes.c_void_p]
        self.gdi.CreateBitmap.restype=W.HBITMAP
        r,g,b,a=image.split()
        data=Image.merge('RGBA',(ImageChops.multiply(r,a),ImageChops.multiply(g,a),ImageChops.multiply(b,a),a)).tobytes('raw','BGRA')
        bits=ctypes.c_void_p();hd=Header(ctypes.sizeof(Header),image.width,-image.height,1,32,0,len(data),0,0,0,0)
        bitmap=self.gdi.CreateDIBSection(None,ctypes.byref(hd),0,ctypes.byref(bits),None,0)
        if not bitmap:raise ctypes.WinError()
        mask=self.gdi.CreateBitmap(image.width,image.height,1,1,None)
        try:
            ctypes.memmove(bits,data,len(data))
            icon=self.user.CreateIconIndirect(ctypes.byref(IconInfo(True,0,0,mask,bitmap)))
            if not icon:raise ctypes.WinError()
            return icon
        finally:
            if mask:self.gdi.DeleteObject(mask)
            self.gdi.DeleteObject(bitmap)

    def update(self,view,colors,force=False):
        state=(view,dict(colors))
        if state==self.last and self.added and not force:return
        size=max(16,self.user.GetSystemMetrics(49))
        icon=self.make_icon(render_tray(view,colors,size))
        n=self.Notify();n.cbSize=ctypes.sizeof(n);n.hWnd=self.surface.hwnd;n.uID=1
        n.uFlags=1|2|4;n.uCallbackMessage=self.MESSAGE;n.hIcon=icon
        parts=[]
        for key,row in view['windows'].items():
            value=row.get('remaining');parts.append(f'{key}: {value:g}%' if value is not None else f'{key}: 未知')
        n.szTip=('Codex 剩余额度 · '+' / '.join(parts))[:127]
        ok=self.shell.Shell_NotifyIconW(1 if self.added else 0,ctypes.byref(n))
        if not ok:
            self.user.DestroyIcon(icon)
            return False
        old=self.icon;self.icon=icon;self.added=True;self.last=state
        if old:self.user.DestroyIcon(old)
        return True

    def close(self):
        n=self.Notify();n.cbSize=ctypes.sizeof(n);n.hWnd=self.surface.hwnd;n.uID=1
        if self.added:self.shell.Shell_NotifyIconW(2,ctypes.byref(n))
        if self.icon:self.user.DestroyIcon(self.icon)
        self.icon=None;self.added=False
