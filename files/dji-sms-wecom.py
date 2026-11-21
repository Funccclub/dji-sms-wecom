#!/usr/bin/python3
"""Poll a QDC507 AT port and forward newly observed SMS to WeCom."""
import hashlib
import json
import os
import re
import select
import sqlite3
import sys
import termios
import time
import tty
import urllib.parse
import urllib.request

CONFIG = '/etc/dji-sms-wecom.json'
DB = '/etc/dji-sms-wecom-state.sqlite3'
GSM = '@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !"#¤%&\'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà'
EXT = {10:'\f', 20:'^', 40:'{', 41:'}', 47:'\\', 60:'[', 61:'~', 62:']', 64:'|', 101:'€'}

def digits(data, count):
    return ''.join(str(x) for b in data for x in (b & 15, b >> 4))[:count]

def decode_pdu(hextext):
    b = bytes.fromhex(hextext)
    i = 1 + b[0]
    first = b[i]; i += 1
    if first & 3 != 0:
        raise ValueError('not SMS-DELIVER')
    n = b[i]; toa = b[i + 1]; i += 2
    if toa & 0x70 == 0x50:
        raise ValueError('alphanumeric sender not supported')
    sender = ('+' if toa & 0x70 == 0x10 else '') + digits(b[i:i+(n+1)//2], n)
    i += (n + 1) // 2
    pid, dcs = b[i:i+2]; i += 2
    timestamp = b[i:i+7].hex(); i += 7
    length = b[i]; i += 1
    ud = b[i:]
    offset = 0; ref = None; total = 1; part = 1
    if first & 0x40:
        offset = ud[0] + 1
        if offset > len(ud): raise ValueError('invalid UDH')
        p = 1
        while p + 2 <= offset:
            kind, size = ud[p:p+2]; p += 2
            val = ud[p:p+size]; p += size
            if len(val) != size or p > offset: raise ValueError('invalid UDH element')
            if kind == 0 and size == 3: ref, total, part = val
            if kind == 8 and size == 4: ref, total, part = (val[0]*256+val[1], val[2], val[3])
            if kind in (0x24, 0x25): raise ValueError('national language tables unsupported')
    if not 1 <= part <= total <= 255: raise ValueError('invalid multipart sequence')
    alphabet = dcs & 0x0c
    if dcs & 0x20: raise ValueError('compressed SMS unsupported')
    if alphabet == 8:
        if length > len(ud): raise ValueError('truncated SMS')
        text = ud[offset:length].decode('utf-16-be')
    elif alphabet == 0:
        header_septets = (offset * 8 + 6) // 7
        start = header_septets * 7
        count = length - header_septets
        if count < 0 or start + count * 7 > len(ud) * 8: raise ValueError('truncated SMS')
        values = [sum(((ud[(start+j*7+k)//8] >> ((start+j*7+k)%8)) & 1) << k for k in range(7)) for j in range(count)]
        out = []; escaped = False
        for v in values:
            if escaped: out.append(EXT.get(v, '�')); escaped = False
            elif v == 27: escaped = True
            else: out.append(GSM[v])
        text = ''.join(out)
    else:
        raise ValueError('binary or unsupported SMS alphabet')
    return dict(sender=sender, timestamp=timestamp, text=text, ref=ref, total=total, part=part)

class Modem:
    def __enter__(self):
        self.fd = os.open(cfg['port'], os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self.old = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        a = termios.tcgetattr(self.fd); a[4] = a[5] = termios.B115200
        termios.tcsetattr(self.fd, termios.TCSANOW, a)
        return self
    def __exit__(self, *args):
        termios.tcsetattr(self.fd, termios.TCSANOW, self.old); os.close(self.fd)
    def at(self, command):
        termios.tcflush(self.fd, termios.TCIFLUSH)
        os.write(self.fd, (command+'\r').encode())
        data = b''; deadline = time.monotonic()+20
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.fd], [], [], max(0, deadline-time.monotonic()))
            if ready:
                data += os.read(self.fd, 65536)
                if b'\r\nOK\r\n' in data: return data.decode('ascii', 'replace')
                if b'\r\nERROR\r\n' in data or b'+CMS ERROR:' in data: raise RuntimeError('AT command rejected: '+command)
        raise TimeoutError('AT command timed out: '+command)
    def list_sms(self):
        self.at('AT')
        mode = self.at('AT+CMGF?')
        if not re.search(r'\+CMGF:\s*[01]', mode): raise RuntimeError('unknown CMGF')
        old = int(re.search(r'\+CMGF:\s*([01])', mode).group(1))
        try:
            if old: self.at('AT+CMGF=0')
            result = self.at('AT+CMGL=4')
        finally:
            if old: self.at('AT+CMGF=1')
        return re.findall(r'\+CMGL:\s*\d+,[^\r\n]*\r?\n([0-9A-Fa-f]+)', result)

def send(cfg, content):
    params = urllib.parse.urlencode({'corpid':cfg['corp_id'], 'corpsecret':cfg['corp_secret']})
    with urllib.request.urlopen('https://qyapi.weixin.qq.com/cgi-bin/gettoken?'+params, timeout=15) as r:
        token = json.load(r)
    if token.get('errcode') != 0: raise RuntimeError('WeCom gettoken error %s' % token.get('errcode'))
    body = json.dumps({'touser':cfg['to_user'], 'msgtype':'text', 'agentid':int(cfg['agent_id']), 'text':{'content':content}, 'safe':0}, ensure_ascii=False).encode()
    req = urllib.request.Request('https://qyapi.weixin.qq.com/cgi-bin/message/send?'+urllib.parse.urlencode({'access_token':token['access_token']}), data=body, headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=15) as r: result = json.load(r)
    if result.get('errcode') != 0 or result.get('invaliduser'):
        raise RuntimeError('WeCom send rejected: %s' % result.get('errcode'))

def poll(db, cfg):
    with Modem() as modem: messages = modem.list_sms()
    baseline = db.execute("SELECT value FROM meta WHERE key='initialized'").fetchone() is None
    with db:
        for pdu in messages:
            h = hashlib.sha256(bytes.fromhex(pdu)).hexdigest()
            try:
                m = decode_pdu(pdu)
            except (ValueError, IndexError, UnicodeError) as e:
                print('Unsupported or invalid SMS, retained on modem:', type(e).__name__, flush=True)
                continue
            db.execute('INSERT OR IGNORE INTO sms (hash,sender,stamp,ref,total,part,body,done) VALUES (?,?,?,?,?,?,?,?)', (h,m['sender'],m['timestamp'],m['ref'],m['total'],m['part'],m['text'], int(baseline)))
        if baseline: db.execute("INSERT INTO meta VALUES ('initialized','1')")
    if baseline:
        print('Initial SMS baseline saved; existing messages will not be forwarded.', flush=True)
        return
    rows = db.execute('SELECT hash,sender,stamp,ref,total,part,body FROM sms WHERE done=0 ORDER BY rowid').fetchall()
    groups = {}
    for row in rows:
        h, sender, stamp, ref, total, part, body = row
        key = (sender, ref, total) if ref is not None else (h,)
        groups.setdefault(key, []).append(row)
    for group in groups.values():
        total = group[0][4]
        parts = {r[5]:r for r in group}
        if len(group) != total or set(parts) != set(range(1,total+1)):
            continue
        text = ''.join(parts[i][6] for i in range(1,total+1))
        send(cfg, '短信来自：%s\n%s' % (group[0][1],text))
        with db: db.executemany('UPDATE sms SET done=1 WHERE hash=?', [(r[0],) for r in group])
        print('SMS forwarded (%d part(s))' % total, flush=True)

def main():
    global cfg
    os.umask(0o077)
    with open(CONFIG, encoding='utf-8') as f: cfg = json.load(f)
    if not all(cfg.get(k) for k in ('port','corp_id','corp_secret','agent_id','to_user')):
        raise RuntimeError('Configure port and WeCom credentials before starting')
    db = sqlite3.connect(DB)
    db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS sms (hash TEXT PRIMARY KEY,sender TEXT,stamp TEXT,ref INTEGER,total INTEGER,part INTEGER,body TEXT,done INTEGER)')
    db.commit()
    while True:
        try: poll(db, cfg)
        except Exception as e: print('Poll failed:', type(e).__name__, str(e).split('?')[0], flush=True)
        time.sleep(max(10, int(cfg.get('poll_seconds',30))))

if __name__ == '__main__': main()
