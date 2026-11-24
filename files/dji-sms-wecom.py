#!/usr/bin/python3
"""Poll a QDC507 AT port and forward newly observed SMS to WeCom."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import base64
import fcntl
import hashlib
import json
import os
import re
import select
import sqlite3
import subprocess
import sys
import termios
import time
import tty
import urllib.parse
import urllib.request

CONFIG = '/etc/dji-sms-wecom.json'
DB = '/etc/dji-sms-wecom-state.sqlite3'
RETENTION_SECONDS = 7 * 24 * 60 * 60
HISTORY_PAGE_SIZE = 25
MAX_ROW_ID = 2**63 - 1
OUTBOX = '/tmp/dji-sms-wecom-outbox'
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
        self.lock = open('/var/lock/dji-sms-wecom.lock', 'a')
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        try:
            self.fd = os.open(cfg['port'], os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            try:
                self.old = termios.tcgetattr(self.fd)
                tty.setraw(self.fd)
                a = termios.tcgetattr(self.fd); a[4] = a[5] = termios.B115200
                termios.tcsetattr(self.fd, termios.TCSANOW, a)
            except Exception:
                os.close(self.fd)
                raise
        except Exception:
            self.lock.close()
            raise
        return self
    def __exit__(self, *args):
        try:
            termios.tcsetattr(self.fd, termios.TCSANOW, self.old)
        finally:
            os.close(self.fd)
            self.lock.close()
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

    def submit_sms(self, number, content):
        encoded = content.encode('utf-16-be')
        digits_only = number.lstrip('+')
        swapped = ''.join((digits_only + ('F' if len(digits_only) % 2 else ''))[i:i+2][::-1]
                          for i in range(0, len(digits_only), 2))
        tpdu = bytes.fromhex('1100') + bytes([len(digits_only), 0x91 if number.startswith('+') else 0x81])
        tpdu += bytes.fromhex(swapped) + bytes.fromhex('0008AA') + bytes([len(encoded)]) + encoded
        self.at('AT')
        self.at('AT+CMGF=0')
        termios.tcflush(self.fd, termios.TCIFLUSH)
        os.write(self.fd, ('AT+CMGS=%d\r' % len(tpdu)).encode())
        self.read_until((b'> ',), 20)
        os.write(self.fd, b'00' + tpdu.hex().upper().encode() + b'\x1a')
        try:
            result = self.read_until((b'\r\nOK\r\n',), 120)
        except TimeoutError:
            raise RuntimeError('提交后等待模块响应超时，发送结果未知，请先确认收件情况')
        if not re.search(rb'\+CMGS:\s*\d+', result):
            raise RuntimeError('模块响应不完整，发送结果未知，请先确认收件情况')

    def read_until(self, endings, timeout):
        data = b''
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.fd], [], [], max(0, deadline - time.monotonic()))
            if ready:
                data += os.read(self.fd, 4096)
                if any(end in data for end in endings):
                    return data
                if b'\r\nERROR\r\n' in data or b'+CMS ERROR:' in data or b'+CME ERROR:' in data:
                    raise RuntimeError('模块拒绝发送短信')
        raise TimeoutError('模块响应超时')

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
    now = int(time.time())
    with db:
        db.execute('INSERT OR IGNORE INTO expired_sms (hash) SELECT hash FROM sms WHERE expires_at <= ?', (now,))
        removed = db.execute('DELETE FROM sms WHERE expires_at <= ?', (now,)).rowcount
        db.execute("DELETE FROM outgoing_sms WHERE created_at <= ? AND status IN ('sent','failed')",
                   (now - RETENTION_SECONDS,))
    if removed:
        print('Expired SMS records removed: %d' % removed, flush=True)

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
            expires_at = sms_expiry(m['timestamp'], now)
            if expires_at <= now:
                continue
            db.execute(
                'INSERT OR IGNORE INTO sms (hash,sender,stamp,ref,total,part,body,done,delivery_status,expires_at) '
                'SELECT ?,?,?,?,?,?,?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM expired_sms WHERE hash=?)',
                (h, m['sender'], m['timestamp'], m['ref'], m['total'], m['part'],
                 m['text'], int(baseline), 'baseline' if baseline else None, expires_at, h),
            )
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
        with db:
            db.executemany("UPDATE sms SET done=1, delivery_status='sent' WHERE hash=?",
                           [(r[0],) for r in group])
        print('SMS forwarded (%d part(s))' % total, flush=True)

def init_db(db):
    db.execute('PRAGMA secure_delete=ON')
    with db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)')
        db.execute(
            'CREATE TABLE IF NOT EXISTS sms (hash TEXT PRIMARY KEY,sender TEXT,stamp TEXT,'
            'ref INTEGER,total INTEGER,part INTEGER,body TEXT,done INTEGER,delivery_status TEXT,expires_at INTEGER)'
        )
        columns = {column[1] for column in db.execute('PRAGMA table_info(sms)')}
        if 'delivery_status' not in columns:
            db.execute('ALTER TABLE sms ADD COLUMN delivery_status TEXT')
        if 'expires_at' not in columns:
            db.execute('ALTER TABLE sms ADD COLUMN expires_at INTEGER')
            now = int(time.time())
            db.executemany(
                'UPDATE sms SET expires_at=? WHERE rowid=?',
                [(sms_expiry(stamp, now), rowid) for rowid, stamp in db.execute('SELECT rowid,stamp FROM sms')],
            )
        db.execute('CREATE INDEX IF NOT EXISTS sms_expiry_index ON sms(expires_at)')
        # The modem may keep returning expired records. Keep only the deduplication hashes.
        db.execute('CREATE TABLE IF NOT EXISTS expired_sms (hash TEXT PRIMARY KEY) WITHOUT ROWID')
        db.execute('CREATE TABLE IF NOT EXISTS outgoing_sms (job TEXT PRIMARY KEY, number TEXT NOT NULL, '
                   'body TEXT NOT NULL, created_at INTEGER NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL)')


def parse_sms_time(stamp):
    # The modem stores the timestamp in swapped BCD, with a signed quarter-hour offset.
    if not stamp or not re.fullmatch(r'[0-9]{12}[0-9a-f][0-9]', stamp):
        return None
    try:
        year, month, day, hour, minute, second = (
            int(stamp[i:i + 2][::-1]) for i in range(0, 12, 2)
        )
        units = int(stamp[12], 16)
        offset = (int(stamp[13]) * 10 + (units & 7)) * (-15 if units & 8 else 15)
        return datetime(2000 + year, month, day, hour, minute, second,
                        tzinfo=timezone(timedelta(minutes=offset)))
    except ValueError:
        return None


def sms_time(stamp):
    value = parse_sms_time(stamp)
    return value.isoformat() if value else None


def sms_expiry(stamp, fallback_time):
    value = parse_sms_time(stamp)
    return (int(value.timestamp()) if value else fallback_time) + RETENTION_SECONDS


def sms_history(cursor):
    upper_id = MAX_ROW_ID
    if cursor != 'latest':
        if not re.fullmatch(r'[1-9][0-9]{0,18}', cursor) or int(cursor) > MAX_ROW_ID:
            raise ValueError('Invalid history cursor')
        upper_id = int(cursor) - 1

    page = {'records': [], 'next_cursor': None}
    if not os.path.exists(DB):
        return page

    with closing(sqlite3.connect('file:' + DB + '?mode=ro', uri=True)) as db:
        columns = {column[1] for column in db.execute('PRAGMA table_info(sms)')}
        if not columns:
            return page
        delivery = 'delivery_status' if 'delivery_status' in columns else 'NULL'
        active_filter = ' AND expires_at > ?' if 'expires_at' in columns else ''
        params = (int(time.time()),) if active_filter else ()
        rows = db.execute(
            f'SELECT rowid,sender,stamp,ref,total,part,body,done,{delivery} FROM sms '
            f'WHERE 1=1{active_filter} ORDER BY rowid',
            params,
        ).fetchall()

    groups = []
    candidates = {}
    for rowid, sender, stamp, ref, total, part, body, done, status in rows:
        timestamp = parse_sms_time(stamp)
        key = (sender, ref, total)
        group = None
        if ref is not None and total > 1:
            for candidate in reversed(candidates.get(key, [])):
                other = candidate['timestamp']
                close = abs((timestamp - other).total_seconds()) <= 600 if timestamp and other else stamp == candidate['stamp']
                if close and part not in candidate['parts']:
                    group = candidate
                    break
        if group is None:
            group = {'rowid': rowid, 'sender': sender, 'stamp': stamp, 'timestamp': timestamp,
                     'total': total, 'parts': {}, 'statuses': []}
            groups.append(group)
            if ref is not None and total > 1:
                candidates.setdefault(key, []).append(group)
        group['rowid'] = max(group['rowid'], rowid)
        group['parts'][part] = body
        group['statuses'].append(status or ('unknown' if done else 'pending'))

    groups = sorted((group for group in groups if group['rowid'] <= upper_id),
                    key=lambda group: group['rowid'], reverse=True)
    for group in groups[:HISTORY_PAGE_SIZE]:
        parts = group['parts']
        statuses = set(group['statuses'])
        record = {
            'sender': group['sender'], 'time': sms_time(group['stamp']), 'total': group['total'],
            'body': ''.join(parts.get(index, '［缺少第 %d 段］' % index)
                            for index in range(1, group['total'] + 1)),
            'status': next(iter(statuses)) if len(statuses) == 1 else 'mixed',
            'received_parts': len(parts),
        }
        proposed = page['records'] + [record]
        if page['records'] and len(json.dumps(proposed, ensure_ascii=False).encode('utf-8')) > 48000:
            break
        page['records'].append(record)
    if len(groups) > len(page['records']):
        page['next_cursor'] = str(groups[len(page['records']) - 1]['rowid'])
    return page


def outbox_path(job):
    if not re.fullmatch(r'[0-9a-f]{32}', job):
        raise ValueError('Invalid request')
    os.makedirs(OUTBOX, mode=0o700, exist_ok=True)
    return os.path.join(OUTBOX, job)


def submit_job(job, number, payload):
    path = outbox_path(job)
    if not re.fullmatch(r'\+?[0-9]{1,20}', number):
        raise ValueError('手机号格式不正确')
    content = base64.b64decode(payload, validate=True).decode('utf-8')
    if not content.strip() or len(content.encode('utf-16-be')) > 140:
        raise ValueError('短信内容须为 1 至 70 个中文字或等量字符')
    with closing(sqlite3.connect(DB)) as db:
        init_db(db)
        with db:
            added = db.execute('INSERT OR IGNORE INTO outgoing_sms VALUES (?, ?, ?, ?, ?, ?)',
                               (job, number, content, int(time.time()), 'queued', '')).rowcount
        if not added:
            return
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        update_job(path, 'failed', '请求已存在，发送结果未知')
        return
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump({'status': 'queued', 'number': number, 'content': content}, f)
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), 'send-worker', job],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
    except Exception:
        os.unlink(path)
        with closing(sqlite3.connect(DB)) as db, db:
            db.execute("UPDATE outgoing_sms SET status='failed', message=? WHERE job=?", ('无法启动发送任务', job))
        raise


def update_job(path, status, message=''):
    with closing(sqlite3.connect(DB)) as db, db:
        db.execute('UPDATE outgoing_sms SET status=?, message=? WHERE job=?',
                   (status, message, os.path.basename(path)))
    temporary = path + '.new'
    with open(temporary, 'w', encoding='utf-8') as f:
        json.dump({'status': status, 'message': message}, f)
    os.replace(temporary, path)


def send_worker(job):
    global cfg
    path = outbox_path(job)
    try:
        with open(path, encoding='utf-8') as f:
            request = json.load(f)
        with open(CONFIG, encoding='utf-8') as f:
            cfg = json.load(f)
        update_job(path, 'sending')
        with Modem() as modem:
            modem.submit_sms(request['number'], request['content'])
        update_job(path, 'sent', '模块已接受短信')
    except Exception as error:
        update_job(path, 'failed', str(error))


def job_status(job):
    path = outbox_path(job)
    with closing(sqlite3.connect(DB)) as db:
        row = db.execute('SELECT status,message FROM outgoing_sms WHERE job=?', (job,)).fetchone()
    if row:
        return {'status': row[0], 'message': row[1]}
    with open(path, encoding='utf-8') as f:
        request = json.load(f)
    return {'status': request['status'], 'message': request.get('message', '')}


def outgoing_history(cursor):
    if cursor != 'latest' and (not re.fullmatch(r'[1-9][0-9]{0,18}', cursor) or int(cursor) > MAX_ROW_ID):
        raise ValueError('Invalid history cursor')
    upper_id = MAX_ROW_ID if cursor == 'latest' else int(cursor) - 1
    if not os.path.exists(DB):
        return {'records': [], 'next_cursor': None}
    with closing(sqlite3.connect('file:' + DB + '?mode=ro', uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outgoing_sms'").fetchone():
            return {'records': [], 'next_cursor': None}
        rows = db.execute(
            'SELECT rowid,number,body,created_at,status,message FROM outgoing_sms '
            'WHERE rowid <= ? AND (created_at > ? OR status NOT IN (?, ?)) ORDER BY rowid DESC LIMIT ?',
            (upper_id, int(time.time()) - RETENTION_SECONDS, 'sent', 'failed', HISTORY_PAGE_SIZE + 1),
        ).fetchall()
    return {'records': [
        {'number': number, 'body': body, 'time': datetime.fromtimestamp(created).astimezone().isoformat(),
         'status': status, 'message': message}
        for _, number, body, created, status, message in rows[:HISTORY_PAGE_SIZE]
    ], 'next_cursor': str(rows[HISTORY_PAGE_SIZE - 1][0]) if len(rows) > HISTORY_PAGE_SIZE else None}


def main():
    global cfg
    os.umask(0o077)
    with open(CONFIG, encoding='utf-8') as f: cfg = json.load(f)
    if not all(cfg.get(k) for k in ('port','corp_id','corp_secret','agent_id','to_user')):
        raise RuntimeError('Configure port and WeCom credentials before starting')
    db = sqlite3.connect(DB)
    init_db(db)
    while True:
        try: poll(db, cfg)
        except Exception as e: print('Poll failed:', type(e).__name__, str(e).split('?')[0], flush=True)
        time.sleep(max(10, int(cfg.get('poll_seconds',30))))

if __name__ == '__main__':
    if len(sys.argv) == 5 and sys.argv[1] == 'submit-sms':
        try:
            submit_job(*sys.argv[2:])
            print(json.dumps({'status': 'queued'}))
        except (ValueError, UnicodeError, OSError) as error:
            print(str(error), file=sys.stderr)
            sys.exit(1)
    elif len(sys.argv) == 3 and sys.argv[1] == 'send-worker':
        send_worker(sys.argv[2])
    elif len(sys.argv) == 3 and sys.argv[1] == 'send-status':
        try:
            print(json.dumps(job_status(sys.argv[2])))
        except (ValueError, OSError) as error:
            print(str(error), file=sys.stderr)
            sys.exit(1)
    elif len(sys.argv) == 3 and sys.argv[1] == 'outgoing-history':
        try:
            print(json.dumps(outgoing_history(sys.argv[2])))
        except (ValueError, sqlite3.Error) as error:
            print(str(error), file=sys.stderr)
            sys.exit(1)
    elif len(sys.argv) == 3 and sys.argv[1] == 'history':
        try:
            print(json.dumps(sms_history(sys.argv[2]), ensure_ascii=False))
        except (ValueError, sqlite3.Error) as error:
            print(str(error), file=sys.stderr)
            sys.exit(1)
    elif len(sys.argv) == 1:
        main()
    else:
        sys.exit(2)
