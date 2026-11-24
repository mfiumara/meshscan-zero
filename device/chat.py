# SPDX-License-Identifier: GPL-3.0-or-later
"""Cardputer chat: local control API, keyboard and 320x170 framebuffer view."""
import json
import os
import threading
import time
import urllib.error
import urllib.request
from PIL import Image, ImageDraw

# The tca8418c driver emits real shift/arrow keys; the Sym layer uses these
# placeholder evdev codes (the Cardputer Zero console keymap assigns the glyphs).
TEXT_KEYS = {**{2+i:c for i,c in enumerate('1234567890')},
             **{16+i:c for i,c in enumerate('qwertyuiop')},
             **{30+i:c for i,c in enumerate('asdfghjkl')},
             **{44+i:c for i,c in enumerate('zxcvbnm')}, 57:' '}
SYMBOL_KEYS = dict(zip((26,27,39,40,41,43,51,52,53,94,55,69,70,71,72,73,74,75,76,77,79,80,81,82,83,85,86,89,90,91,92,93),
                       ('!','@','#','$','%','^','&','*','(',')','~','`','_','-','+','=','[',']','{','}',';',':',"'",'"','<','>','\\','|',',','.','/','?')))
TOKEN_PATH = '/var/lib/meshscan/ctl/local.token'
BASE = 'http://127.0.0.1:8080'


def post(path, body):
    with open(TOKEN_PATH, encoding='ascii') as f:
        token = f.read().strip()
    req = urllib.request.Request(BASE+path, json.dumps(body).encode(), method='POST',
                                 headers={'Content-Type':'application/json', 'X-Meshscan-Local-Token':token})
    try:
        with urllib.request.urlopen(req, timeout=4) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.load(exc).get('error', 'Request failed')
        except (ValueError, OSError):
            detail = 'Request failed'
        raise RuntimeError(str(detail)) from None


def char_for_key(code, shift=False, caps=False):
    char = SYMBOL_KEYS.get(code, TEXT_KEYS.get(code, ''))
    if char.isalpha() and (shift != caps):
        return char.upper()
    return char


def wrap(text, font, width):
    words = str(text or '').replace('\n', ' ').split()
    lines, line = [], ''
    for word in words:
        while word and font.getlength(word) > width:
            part = ''
            while word and font.getlength(part+word[0]) <= width:
                part, word = part+word[0], word[1:]
            if line:
                lines.append(line); line = ''
            lines.append(part)
        candidate = (line+' '+word).strip()
        if line and font.getlength(candidate) > width:
            lines.append(line); line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return lines or ['']


class Chat:
    def __init__(self, data, ui):
        self.data, self.ui = data, ui
        self.channel = 0
        self.text = ''
        self.offset = 0
        self.previous_count = 0
        self.caps = False
        self.sending = False
        self.notice = ''
        self.pending_mode = False
        self.mode_started = 0.0
        self.lock = threading.Lock()
        self.cached_image = None
        self.cached_signature = None

    def channels(self):
        with self.data.lock:
            channels = self.data.settings.get('channels') or []
        return ['Public'] + [str(x.get('name')) for x in channels if isinstance(x, dict) and x.get('name')]

    def submit(self):
        channels = self.channels()
        if self.sending or not self.text.strip() or self.channel >= len(channels):
            return
        text, index = self.text, self.channel
        # Optional fail-closed safety interlock for a private-channel test run.
        private_only = os.environ.get('MESHCHAT_PRIVATE_ONLY')
        if private_only:
            with self.data.lock:
                extras = self.data.settings.get('channels') or []
            if (index < 1 or index > len(extras) or not isinstance(extras[index-1], dict)
                    or extras[index-1].get('name') != private_only or not extras[index-1].get('private')):
                self.notice = 'Private-channel safety lock'
                return
        self.sending, self.notice = True, 'Sending…'
        def worker():
            try:
                post('/api/send', {'text':text, 'channel':index})
                with self.lock:
                    if self.text == text:
                        self.text = ''
                    self.notice = 'Queued'
                    self.offset = 0
            except (OSError, RuntimeError, ValueError) as exc:
                with self.lock:
                    self.notice = 'Failed: '+str(exc)
            finally:
                with self.lock:
                    self.sending = False
        threading.Thread(target=worker, daemon=True, name='chat-send').start()

    def switch_mode(self):
        if self.pending_mode:
            return
        self.pending_mode, self.notice = True, 'Radio restarting…'
        self.mode_started = time.monotonic()
        def worker():
            try:
                post('/api/mode', {'mode':'chat'})
            except (OSError, RuntimeError, ValueError) as exc:
                with self.lock:
                    self.notice = 'Failed: '+str(exc)
                    self.pending_mode = False
        threading.Thread(target=worker, daemon=True, name='chat-mode').start()

    def handle(self, code, value, shift):
        """Return True to leave chat; printable keys only ever edit the composer."""
        if value not in (1,2):
            return False
        if code == 1:
            return True
        with self.data.lock:
            settings, mode, online = self.data.settings, self.data.status.get('mode'), self.data.online
        if not online or not settings.get('configured'):
            return False
        if mode != 'chat':
            if code in (28,96) and value == 1:
                self.switch_mode()
            return False
        if self.pending_mode:
            self.pending_mode = False
            if self.notice == 'Radio restarting…':
                self.notice = ''
        if code == 58 and value == 1:
            self.caps = not self.caps
        elif code in (15,105,106) and value == 1:
            count = len(self.channels())
            self.channel = (self.channel + (-1 if code == 105 else 1)) % count
            self.offset = self.previous_count = 0
            self.notice = ''
        elif code == 103:
            self.offset += 1
        elif code == 108:
            self.offset = max(0, self.offset-1)
        elif code == 14:
            self.text = self.text[:-1]
        elif code in (28,96) and value == 1:
            self.submit()
        else:
            char = char_for_key(code, shift, self.caps)
            if char and len((self.text+char).encode('utf-8')) <= 140:
                self.text += char
                self.notice = ''
        return False

    def draw(self, url):
        # Message rows and text rasterisation are expensive on the CM0; redraw only
        # when something visible changes, not on every input polling frame.
        with self.data.lock:
            settings, status, online = self.data.settings, self.data.status, self.data.online
            channels = tuple((c.get('name'), c.get('private')) for c in (settings.get('channels') or []) if isinstance(c, dict))
            rows = tuple((m.get('id'), m.get('updated'), m.get('status'), m.get('repeats'), m.get('text'),
                          m.get('channel'), m.get('src'), m.get('ts'), m.get('direction'))
                         for m in self.data.messages if isinstance(m, dict))
        with self.lock:
            if status.get('mode') == 'chat' and self.pending_mode:
                self.pending_mode = False
                if self.notice == 'Radio restarting…':
                    self.notice = ''
            elif self.pending_mode and time.monotonic()-self.mode_started > 30:
                self.pending_mode = False
                self.notice = 'Switch timed out; press Enter to retry'
            signature = (online, settings.get('configured'), status.get('mode'), channels, rows,
                         self.channel, self.text, self.notice, self.offset, self.caps,
                         self.pending_mode, int(time.time()//30), url)
        if signature != self.cached_signature:
            self.cached_image = self._draw(url)
            self.cached_signature = signature
        return self.cached_image

    def _draw(self, url):
        ui = self.ui
        with self.data.lock:
            settings, status, online = self.data.settings, self.data.status, self.data.online
            messages = list(self.data.messages)
        im = Image.new('RGB',(320,170),(6,8,12))
        d = ImageDraw.Draw(im)
        d.text((9,5),'MESH CHAT',font=ui.BOLD,fill=ui.BLUE)
        if not online or not settings.get('configured'):
            d.text((9,32),'Finish setup on your phone' if online else 'Waiting for meshscan…',font=ui.BOLD,fill=ui.TEXT)
            d.text((9,50),'Scan to open the dashboard',font=ui.FONT,fill=ui.MUTED)
            qr = ui.qr_image(url,100)
            im.paste(qr,(210,35))
            d.text((9,79),ui.clip(url,190,ui.MONO),font=ui.MONO,fill=ui.TEXT)
            d.text((9,151),'ESC  BACK TO MAP',font=ui.MONO,fill=ui.MUTED)
            return im
        if status.get('mode') != 'chat':
            d.text((9,36),'Radio is in scan mode',font=ui.BOLD,fill=ui.TEXT)
            d.text((9,58),'ENTER  switch to chat mode',font=ui.FONT,fill=ui.BLUE)
            d.text((9,77),ui.clip(self.notice,280,ui.FONT),font=ui.FONT,fill=(255,184,107))
            d.text((9,151),'ESC  BACK TO MAP',font=ui.MONO,fill=ui.MUTED)
            return im
        channels = self.channels()
        self.channel %= len(channels)
        active = channels[self.channel]
        d.text((128,5),'TAB  CHANNEL',font=ui.SMALL,fill=ui.MUTED)
        d.text((241,5),'ESC  MAP',font=ui.SMALL,fill=ui.MUTED)
        d.rounded_rectangle((8,20,312,39),radius=4,fill=(14,28,37))
        d.text((14,23),ui.clip(active,180,ui.BOLD),font=ui.BOLD,fill=ui.BLUE)
        d.text((260,25),'%d / %d'%(self.channel+1,len(channels)),font=ui.SMALL,fill=ui.MUTED)
        items = sorted([m for m in messages if isinstance(m,dict) and m.get('channel') == active], key=lambda m:m.get('ts') or 0)
        if self.offset and len(items)>self.previous_count:
            self.offset += len(items)-self.previous_count
        self.previous_count = len(items)
        self.offset = min(self.offset, max(0,len(items)-1))
        end = max(0,len(items)-self.offset)
        items = items[max(0,end-12):end]
        y = 121
        for m in reversed(items):
            text = ''.join(c for c in str(m.get('text') or '') if ord(c)<0x10000 and c not in ('\ufe0f','\u200d'))
            lines = wrap(text,ui.FONT,245)[-2:]
            height = 12+12*len(lines)
            if y-height < 41:
                break
            y -= height
            outgoing = m.get('direction') == 'out'
            sender = str(m.get('src') or 'Unknown')
            state = ('heard ×%s' % m.get('repeats')) if outgoing and m.get('repeats') else str(m.get('status') or '') if outgoing else ''
            age = max(0,int(time.time()-(m.get('ts') or time.time())))
            ago = '%dm'%(age//60) if age>=60 else '%ds'%age
            d.text((10,y),ui.clip(sender,150,ui.SMALL),font=ui.SMALL,fill=ui.GREEN if outgoing else ui.BLUE)
            d.text((235,y),ui.clip((state+' '+ago).strip(),75,ui.SMALL),font=ui.SMALL,fill=ui.MUTED)
            for i,line in enumerate(lines):
                d.text((10,y+10+i*12),line,font=ui.FONT,fill=ui.TEXT)
        if not items:
            d.text((10,63),'No messages in this channel',font=ui.FONT,fill=ui.MUTED)
        d.line((8,125,312,125),fill=(38,60,70))
        d.rounded_rectangle((8,130,312,164),radius=4,outline=(40,74,88),fill=(10,19,26))
        with self.lock:
            text, notice = self.text, self.notice
        shown = text
        while shown and ui.FONT.getlength(shown+'|')>250:
            shown = shown[1:]
        d.text((13,132),shown+'|',font=ui.FONT,fill=ui.TEXT)
        d.text((13,150),ui.clip(notice,210,ui.SMALL),font=ui.SMALL,fill=(255,184,107) if notice.startswith('Failed') else ui.MUTED)
        d.text((265,150),'%d/140'%len(text.encode('utf-8')),font=ui.SMALL,fill=ui.MUTED)
        return im
