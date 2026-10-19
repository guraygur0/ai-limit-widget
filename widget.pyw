"""Native high-DPI vector usage card. Qt paints text at the display's pixel ratio."""
from __future__ import annotations
import ctypes
import copy
import json
import math
import os
from pathlib import Path
import sys
import threading
import time
from PySide6.QtCore import Qt, QTimer, Signal, QObject, QRectF, QPointF, QPoint
from PySide6.QtGui import QColor, QPainter, QPainterPath, QLinearGradient, QFont, QFontDatabase, QPen, QImage, QCursor
from PySide6.QtWidgets import QApplication, QWidget, QMenu, QToolTip
import usage

SETTINGS = usage.STATE / 'window.json'
BLUE = QColor('#009cff')
TEXT = QColor('#f5f5f5')
MUTED = QColor('#999999')


def countdown(reset):
    if reset is None:
        return ''
    seconds=max(0,int(reset-time.time()))
    if not seconds:
        return 'Reset pending update'
    days,seconds=divmod(seconds,86400)
    hours,seconds=divmod(seconds,3600)
    minutes=seconds//60
    return f'Resets in {days}d {hours}h' if days else (f'Resets in {hours}h {minutes}m' if hours else f'Resets in {minutes}m')


def settings():
    try:
        raw=json.loads(SETTINGS.read_text(encoding='utf-8'))
    except (OSError,ValueError):
        return {}
    if not isinstance(raw,dict):
        return {}
    result={'scale':max(.5,min(1.5,usage.number(raw.get('scale'),.82))),
            'topmost':raw.get('topmost') is True,
            'position_locked':raw.get('position_locked') is True}
    for key in ('x','y'):
        if key in raw:
            result[key]=int(max(-100000,min(100000,usage.number(raw[key]))))
    return result


class Result(QObject):
    ready=Signal(dict)
    partial=Signal(dict)


class Widget(QWidget):
    def __init__(self, preview=False):
        super().__init__()
        self.preview=preview
        self.options=settings()
        self.scale=self.options.get('scale',.82)
        self.setWindowTitle('Usage Limits')
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        if self.options.get('topmost',False):
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint,True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.data=usage.merge_live(usage.cached())
        self.busy=False
        self.manual_active=False
        self.requested_providers=[]
        self.offset=None
        self.bridge=Result()
        self.bridge.ready.connect(self.receive)
        self.bridge.partial.connect(self.receive_partial)
        self.resize_card()
        area=QApplication.primaryScreen().availableGeometry()
        self.move(self.options.get('x',area.right()-self.width()-30),self.options.get('y',area.top()+70))
        self.ensure_on_screen()
        for screen in QApplication.screens():
            screen.availableGeometryChanged.connect(self.ensure_on_screen)
        QApplication.instance().screenRemoved.connect(self.ensure_on_screen)
        self.timer=QTimer(self)
        self.timer.setTimerType(Qt.TimerType.CoarseTimer)
        self.timer.timeout.connect(self.tick)
        self.desktop_layer=None
        self.last_diagnostic=0
        self.last_paint_tick=None
        if not preview:
            from desktop_layer import DesktopLayer
            self.desktop_layer=DesktopLayer(self)
            self.desktop_timer=QTimer(self)
            self.desktop_timer.timeout.connect(self.desktop_layer.maintain)
            self.desktop_timer.start(250)
            self.timer.start(1000)
            QTimer.singleShot(0,self.refresh)

    def ensure_on_screen(self,*args):
        screens=QApplication.screens()
        if not screens:
            return
        # Preserve a saved secondary-monitor position; recover when that screen is removed.
        center=self.frameGeometry().center()
        screen=next((s for s in screens if s.availableGeometry().contains(center)),None)
        screen=screen or QApplication.primaryScreen()
        area=screen.availableGeometry()
        self.move(max(area.left(),min(self.x(),area.right()-self.width()+1)),
                  max(area.top(),min(self.y(),area.bottom()-self.height()+1)))

    @staticmethod
    def visible_limits(data):
        return usage.display_limits(data)

    def section_height(self,data):
        return 57 + len(self.visible_limits(data))*79 + (30 if not data.get('ok') or data.get('stale') else 0)

    def resize_card(self):
        self.card_height=max(497,34+sum(self.section_height(self.data.get(k,{})) for k in ('codex','claude'))+50)
        self.setFixedSize(round(554*self.scale),round(self.card_height*self.scale))
        self.update()

    def text(self,p,x,y,value,size=19,color=TEXT,bold=False,right=False):
        font=QFont('Segoe UI')
        font.setPixelSize(size)
        font.setWeight(QFont.Weight.DemiBold if bold else QFont.Weight.Normal)
        font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        p.setFont(font)
        p.setPen(color)
        flags=Qt.AlignmentFlag.AlignVCenter | (Qt.AlignmentFlag.AlignRight if right else Qt.AlignmentFlag.AlignLeft)
        p.drawText(QRectF(29 if right else x,y-4,x-29 if right else 500,size+12),flags,value)

    def icon(self,p,provider,x,y):
        p.save()
        p.translate(x,y)
        p.setPen(Qt.PenStyle.NoPen)
        if provider=='claude':
            p.setBrush(QColor('#d77b57'))
            # Claude's pixel crab is intentionally a pixel silhouette; text is vector.
            path=QPainterPath()
            for rect in [QRectF(5,0,20,15),QRectF(0,5,30,5),QRectF(5,15,3,4),QRectF(12,15,3,4),QRectF(22,15,3,4)]:
                path.addRect(rect)
            p.drawPath(path.simplified())
            p.setBrush(QColor('#303030'))
            p.drawRect(QRectF(10,5,2,3))
            p.drawRect(QRectF(19,5,2,3))
        else:
            path=QPainterPath()
            for i in range(181):
                angle=i*math.tau/180
                r=11.5+1.5*math.cos(angle*6)
                point=QPointF(15+r*math.cos(angle),12+r*math.sin(angle))
                if i==0:
                    path.moveTo(point)
                else:
                    path.lineTo(point)
            gradient=QLinearGradient(0,0,25,25)
            gradient.setColorAt(0,QColor('#a6b0ff'))
            gradient.setColorAt(1,QColor('#737cff'))
            p.setBrush(gradient)
            p.drawPath(path)
            p.setPen(QPen(QColor('#4249b7'),1.5,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap,Qt.PenJoinStyle.RoundJoin))
            p.drawLine(QPointF(12,10),QPointF(16,14))
            p.drawLine(QPointF(16,14),QPointF(12,17))
            p.drawLine(QPointF(18,17),QPointF(21,17))
        p.restore()

    def reset_icon(self,p,x,y):
        p.save()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(MUTED,1.4,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap,Qt.PenJoinStyle.RoundJoin))
        p.drawArc(QRectF(x+2,y+3,13,14),70*16,280*16)
        p.drawLine(QPointF(x+9,y),QPointF(x+14,y+4))
        p.drawLine(QPointF(x+14,y+4),QPointF(x+9,y+8))
        p.restore()

    def section(self,p,provider,y):
        data=self.data.get(provider,{})
        self.icon(p,provider,29,y+2)
        self.text(p,70,y,'Codex' if provider=='codex' else 'Claude Code',25,bold=True)
        stamp=data.get('updated')
        if self.busy and provider in self.requested_providers:
            status='Refreshing…'
        elif data.get('stale') or not data.get('ok'):
            status=f'Stale · {max(0,int((time.time()-stamp)/60))}m ago' if stamp else 'No data'
        else:
            age=max(0,int(time.time()-stamp)) if stamp else 0
            status=(f'{age}s ago' if age<60 else f'{age//60}m ago') if stamp else ''
            if data.get('source')=='claude-code': status='Local · '+status
        self.text(p,525,y+5,status,13,MUTED,right=True)
        y+=44
        for row in self.visible_limits(data):
            left=max(0,min(100,100-row['used']))
            self.text(p,29,y,row['label'],19 if len(row['label'])<27 else 16)
            self.text(p,525,y,f'{left:.0f}% left',20,bold=True,right=True)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor('#4b4b4d'))
            p.drawRoundedRect(QRectF(29,y+27,496,14),7,7)
            if left>0:
                p.setBrush(BLUE)
                width=496*left/100
                p.drawRoundedRect(QRectF(29,y+27,width,14),min(7,width/2),min(7,width/2))
            reset=countdown(row.get('reset'))
            if reset:
                self.reset_icon(p,29,y+52)
                self.text(p,54,y+51,reset,19,MUTED)
            y+=79
        if not data.get('ok') or data.get('stale'):
            self.text(p,29,y,self.failure_text(data),15,QColor('#d6b17d'))
            y+=30
        return y+13

    @staticmethod
    def failure_text(data,now=None):
        now=time.time() if now is None else now
        code=data.get('http_status')
        reason={429:'Rate limited',401:'Sign in again',403:'Access unavailable'}.get(code)
        if reason is None:
            reason={'Connection unavailable':'Connection unavailable','Request timed out':'Request timed out',
                    'Usage unavailable':'Usage unavailable','Sign in again':'Sign in again'}.get(data.get('error'),'Waiting for data')
        retry=max(data.get('retry_at',0),data.get('cooldown_until',0),data.get('server_until',0))
        wait=max(0,math.ceil(retry-now))
        return f'{reason} · Retry in {wait//60}:{wait%60:02d}' if wait else reason

    def update_details(self):
        now=time.time()
        lines=[]
        for key,name in (('codex','Codex'),('claude','Claude')):
            item=self.data.get(key,{})
            stamp=item.get('updated')
            age=f'{max(0,int(now-stamp))//60}m ago' if stamp else 'not yet received'
            wait=max(0,math.ceil(usage._next_attempt.get(key,now)-now))
            source='Claude Code local data' if item.get('source')=='claude-code' else 'server response'
            lines.append(f'{name}: last successful update {age}; source: {source}; refresh interval: {usage.poll_interval(key)//60}m.')
            if not item.get('ok'):
                lines.append(self.failure_text(item))
            else:
                lines.append(f'Next automatic refresh: {wait//60}:{wait%60:02d}')
        self.setToolTip('\n'.join(lines))

    def paintEvent(self,event):
        p=QPainter(self)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        p.scale(self.width()/554,self.height()/self.card_height)
        path=QPainterPath()
        path.addRoundedRect(QRectF(.6,.6,552.8,self.card_height-1.2),52,52)
        gradient=QLinearGradient(0,0,0,self.card_height*.68)
        gradient.setColorAt(0,QColor('#313131'))
        gradient.setColorAt(1,QColor('#1d1d1d'))
        p.fillPath(path,gradient)
        p.setPen(QPen(QColor(110,110,110,100),1))
        p.drawPath(path)
        y=self.section(p,'codex',34)
        p.setPen(QPen(QColor('#3b3b3b'),1))
        p.drawLine(QPointF(29,y+5),QPointF(525,y+5))
        self.section(p,'claude',y+29)
        p.end()

    def mousePressEvent(self,event):
        if event.button()==Qt.MouseButton.LeftButton and not self.options.get('position_locked',False):
            self.offset=event.globalPosition().toPoint()-self.pos()

    def mouseMoveEvent(self,event):
        if self.offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint()-self.offset)

    def mouseReleaseEvent(self,event):
        self.offset=None
        self.save()

    def mouseDoubleClickEvent(self,event):
        self.refresh(manual=True)

    def save(self):
        self.options.update(x=self.x(),y=self.y(),scale=self.scale,topmost=bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint))
        usage.atomic_json(SETTINGS,self.options)

    def set_size(self,scale):
        self.scale=scale
        self.resize_card()
        self.ensure_on_screen()
        self.save()

    def toggle_top(self):
        self.options['topmost']=not self.options.get('topmost',False)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint,self.options['topmost'])
        self.show()
        self.save()

    def toggle_position_lock(self):
        self.options['position_locked']=not self.options.get('position_locked',False)
        self.offset=None
        self.save()

    def contextMenuEvent(self,event):
        menu=QMenu(self)
        menu.setStyleSheet('QMenu { background:#252525; color:#f5f5f5; padding:6px; } QMenu::item { padding:7px 20px; } QMenu::item:selected { background:#007acc; }')
        menu.addAction('Refresh now',lambda:self.refresh(manual=True))
        lock=menu.addAction('Lock position',self.toggle_position_lock)
        lock.setCheckable(True)
        lock.setChecked(self.options.get('position_locked',False))
        sizes=menu.addMenu('Size')
        for name,scale in [('Small',.65),('Medium',.82),('Large',1.0),('Extra large',1.2)]:
            sizes.addAction(name,lambda s=scale:self.set_size(s))
        top=menu.addAction('Always on top',self.toggle_top)
        top.setCheckable(True)
        top.setChecked(bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint))
        menu.addSeparator()
        for key,name in [('codex','Codex'),('claude','Claude')]:
            stamp=self.data.get(key,{}).get('updated')
            when=time.strftime('%H:%M:%S',time.localtime(stamp)) if stamp else 'not yet available'
            menu.addAction(f'{name} · Last update: {when} · Interval: {usage.poll_interval(key)//60}m').setEnabled(False)
        menu.addSeparator()
        menu.addAction('Close',self.close)
        menu.exec(event.globalPos())

    def refresh(self,manual=False):
        if self.busy:
            if manual:
                QToolTip.showText(QCursor.pos(),'A refresh is already in progress.',self)
            return
        now=time.time()
        requested=[key for key in ('claude','codex') if usage.due(key,manual,now)]
        if manual:
            messages=[]
            for key in ('claude','codex'):
                if key in requested:
                    messages.append(key.title()+': requesting live data…')
                elif max(usage._server_until.get(key,0),usage._cooldown_until.get(key,0))>now:
                    seconds=math.ceil(max(usage._server_until.get(key,0),usage._cooldown_until.get(key,0))-now)
                    messages.append(f'{key.title()}: rate limited; wait {seconds//60}m {seconds%60}s.')
                else:
                    messages.append(key.title()+': just refreshed; please wait a few seconds.')
            QToolTip.showText(QCursor.pos(),'\n'.join(messages),self,msecShowTime=8000)
        if not requested:
            return
        self.requested_providers=requested
        self.batch_providers=list(requested)
        self.manual_active=manual
        self.busy=True
        self.update()
        previous=copy.deepcopy(self.data)
        def worker():
            try:
                data=usage.collect(previous,manual=manual,on_result=self.bridge.partial.emit)
            except Exception:
                data={key: dict(value,ok=False,stale=True,error="Refresh failed") for key,value in previous.items()}
            self.bridge.ready.emit(data)
        threading.Thread(target=worker,daemon=True).start()

    def receive_partial(self,data):
        self.data.update(data)
        self.data=usage.merge_live(self.data)
        for key in data:
            if key in self.requested_providers:
                self.requested_providers.remove(key)
        self.resize_card()
        self.update()

    def receive(self,data):
        self.busy=False
        self.data=usage.merge_live(data)
        self.resize_card()
        if self.manual_active:
            lines=[]
            for key in self.batch_providers:
                item=data.get(key,{})
                if item.get('ok'):
                    stamp=time.strftime('%H:%M:%S',time.localtime(item['updated']))
                    lines.append(f'{key.title()}: updated ({stamp}).')
                else:
                    lines.append(key.title()+': '+item.get('error','Could not fetch data'))
            QToolTip.showText(QCursor.pos(),'\n'.join(lines),self,msecShowTime=8000)
        self.manual_active=False
        self.write_runtime()

    def tick(self):
        usage.reconcile_clock()
        merged=usage.merge_live(self.data)
        if merged is not self.data:
            self.data=merged
            self.resize_card()
            self.write_runtime()
        # Never present an old successful response as current after sleep/network loss.
        changed=False
        for key,item in self.data.items():
            age=time.time()-(item.get('updated') or 0)
            stale=not item.get('ok') or age>90 or age < -30
            if item.get('stale',False)!=stale:
                item['stale']=stale
                changed=True
        if changed:
            self.resize_card()
        # Quota/reset text changes by the minute; only retry countdowns need 1 Hz paint.
        paint_tick=int(time.time()) if any(not v.get('ok') or v.get('stale') for v in self.data.values()) else int(time.time()//5)
        if paint_tick!=self.last_paint_tick:
            self.last_paint_tick=paint_tick
            self.update()
        self.update_details()
        if time.time()-self.last_diagnostic>=60:
            self.last_diagnostic=time.time()
            self.write_runtime()
        if not self.busy and any(usage.due(k) for k in ('claude','codex')):
            self.refresh()

    def write_runtime(self):
        data={'pid':os.getpid(),'renderer':'Qt vector','device_pixel_ratio':self.devicePixelRatioF(),
              'logical_size':[self.width(),self.height()], 'console_handle':int(ctypes.windll.kernel32.GetConsoleWindow()),
              'footer':False,'refresh_intervals_seconds':{k:usage.poll_interval(k) for k in usage.INTERVALS},'checked_at':time.time(),
              'desktop':self.desktop_layer.status() if self.desktop_layer else None,
              'providers':{key:{'updated':value.get('updated'),'ok':value.get('ok'),
                                 'next_attempt':usage._next_attempt.get(key),'stale':value.get('stale',False),
                                 'http_status':value.get('http_status'),'duration':value.get('duration'),
                                 'source':value.get('source','api'),'visible_rows':[row['label'] for row in self.visible_limits(value)]} for key,value in self.data.items()}}
        try:
            usage.atomic_json(usage.STATE/'runtime.json',data)
        except OSError:
            pass


def main():
    preview='--preview' in sys.argv
    if preview:
        os.environ['QT_QPA_PLATFORM']='offscreen'
    app=QApplication(sys.argv[:1])
    for font_file in ('segoeui.ttf','seguisb.ttf'):
        QFontDatabase.addApplicationFont(str(Path('C:/Windows/Fonts')/font_file))
    app.setApplicationName('Usage Limits')
    app.setQuitOnLastWindowClosed(True)
    mutex=None
    if not preview:
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateMutexW.restype=ctypes.c_void_p
        mutex=kernel.CreateMutexW(None,False,'Local\\GurOSUsageWidgetV2')
        if ctypes.get_last_error()==183:
            return
    widget=Widget(preview)
    if preview:
        widget.data=usage.cached()
        widget.scale=1
        widget.resize_card()
        image=QImage(widget.width()*2,widget.height()*2,QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(2)
        image.fill(Qt.GlobalColor.transparent)
        painter=QPainter(image)
        widget.render(painter,QPoint())
        painter.end()
        target=Path(sys.argv[sys.argv.index('--preview')+1])
        target.parent.mkdir(parents=True,exist_ok=True)
        assert image.save(str(target))
        print(json.dumps({'renderer':'Qt vector','preview_dpr':2,'pixels':[image.width(),image.height()],'footer':False}))
        return
    widget.show()
    # Local runtime diagnostics contain no credentials or desktop pixels.
    usage.STATE.mkdir(exist_ok=True)
    widget.desktop_layer.maintain()
    widget.write_runtime()
    app.exec()
    if mutex:
        kernel.CloseHandle.argtypes=[ctypes.c_void_p]
        kernel.CloseHandle(mutex)


if __name__=='__main__':
    main()
