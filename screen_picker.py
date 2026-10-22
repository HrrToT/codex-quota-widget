"""Freeze the virtual desktop for a single screen-color pick; Esc cancels."""
import ctypes
import tkinter as tk
from PIL import ImageGrab, ImageTk

class ScreenPicker:
    def __init__(self,root,done):
        self.done=done
        user=ctypes.windll.user32
        self.left,self.top=user.GetSystemMetrics(76),user.GetSystemMetrics(77)
        self.image=ImageGrab.grab(all_screens=True).convert('RGB')
        self.win=tk.Toplevel(root);self.win.overrideredirect(True)
        self.win.attributes('-topmost',True)
        width,height=self.image.size
        self.win.geometry(f'{width}x{height}{self.left:+d}{self.top:+d}')
        self.photo=ImageTk.PhotoImage(self.image,master=self.win)
        self.canvas=tk.Canvas(self.win,width=width,height=height,highlightthickness=0,cursor='crosshair')
        self.canvas.pack();self.canvas.create_image(0,0,image=self.photo,anchor='nw')
        self.tip=self.canvas.create_text(24,24,text='点击取色 · Esc 取消',anchor='nw',fill='white',font=('Microsoft YaHei UI',12,'bold'))
        self.back=self.canvas.create_rectangle(16,16,340,64,fill='#111923',outline='#94a3b8')
        self.canvas.tag_raise(self.tip)
        self.canvas.bind('<Motion>',self.motion)
        self.canvas.bind('<Button-1>',self.pick)
        self.win.bind('<Escape>',lambda e:self.finish(None))
        self.win.focus_force();self.win.grab_set()
    def color(self,x,y):
        r,g,b=self.image.getpixel((max(0,min(x,self.image.width-1)),max(0,min(y,self.image.height-1))))
        return f'#{r:02x}{g:02x}{b:02x}'
    def motion(self,event):
        color=self.color(event.x,event.y)
        self.canvas.itemconfigure(self.tip,text=f'{color.upper()} · 点击取色 · Esc 取消',fill=color)
    def pick(self,event):self.finish(self.color(event.x,event.y))
    def finish(self,color):
        self.win.grab_release();self.win.destroy();self.done(color)
