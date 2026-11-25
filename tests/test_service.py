import importlib.util
import hashlib
import pathlib
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import closing
from datetime import datetime, timezone
from unittest.mock import patch

if sys.platform == 'win32':
    sys.modules.setdefault('termios', types.ModuleType('termios'))
    sys.modules.setdefault('tty', types.ModuleType('tty'))
    sys.modules.setdefault('fcntl', types.ModuleType('fcntl'))

spec = importlib.util.spec_from_file_location('service', pathlib.Path(__file__).parents[1] / 'files' / 'dji-sms-wecom.py')
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)

def deliver(text, header=b'', stamp=bytes(7)):
    body = header + text.encode('utf-16-be')
    return (bytes([0, 0x40 if header else 0, 4, 0x91, 0x21, 0x43, 0, 8]) + stamp + bytes([len(body)]) + body).hex()

class TestSMS(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        service.init_db(self.db)
    def test_decode_unicode(self):
        x = service.decode_pdu(deliver('测试短信'))
        self.assertEqual((x['sender'], x['text']), ('+1234', '测试短信'))
    def test_decode_gsm7(self):
        # Standard GSM 7-bit packed encoding for "hello".
        pdu = (bytes([0, 0, 4, 0x91, 0x21, 0x43, 0, 0]) + bytes(7) + bytes([5]) + bytes.fromhex('e8329bfd06')).hex()
        self.assertEqual(service.decode_pdu(pdu)['text'], 'hello')
    def test_initial_baseline_and_retry_multipart(self):
        old = deliver('历史')
        p1 = deliver('你好', bytes([5,0,3,42,2,1]))
        p2 = deliver('世界', bytes([5,0,3,42,2,2]))
        class FakeModem:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def list_sms(self): return self.messages
        with patch.object(service, 'Modem', FakeModem), patch.object(service, 'send') as send:
            FakeModem.messages = [old]
            service.poll(self.db, {})
            send.assert_not_called()
            self.assertEqual(self.db.execute('SELECT delivery_status FROM sms').fetchone(), ('baseline',))
            FakeModem.messages = [old,p2]
            service.poll(self.db, {})
            send.assert_not_called()
            FakeModem.messages = [old,p2,p1]
            send.side_effect = RuntimeError('network')
            with self.assertRaises(RuntimeError): service.poll(self.db,{})
            self.assertEqual(self.db.execute('SELECT count(*) FROM sms WHERE done=0').fetchone()[0], 2)
            send.side_effect = None
            service.poll(self.db,{})
            self.assertIn('你好世界', send.call_args.args[1])
            service.poll(self.db,{})
            self.assertEqual(send.call_count, 2)
            self.assertEqual(self.db.execute("SELECT count(*) FROM sms WHERE delivery_status='sent'").fetchone()[0], 2)

    def test_history_pages_and_legacy_migration(self):
        now = datetime(2026, 11, 21, 13, tzinfo=timezone.utc).timestamp()
        clock = patch.object(service.time, 'time', return_value=now)
        clock.start()
        self.addCleanup(clock.stop)
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(service, 'DB', str(pathlib.Path(directory) / 'state.sqlite3')):
                self.assertEqual(service.sms_history('latest'), {'records': [], 'next_cursor': None})
                with closing(sqlite3.connect(service.DB)) as db:
                    db.execute(
                        'CREATE TABLE sms (hash TEXT PRIMARY KEY,sender TEXT,stamp TEXT,'
                        'ref INTEGER,total INTEGER,part INTEGER,body TEXT,done INTEGER)'
                    )
                    db.executemany('INSERT INTO sms VALUES (?, ?, ?, NULL, 1, 1, ?, ?)', [
                        (str(i), '1234', '62111202823023', str(i), i % 2)
                        for i in range(27)
                    ])
                    db.commit()
                    legacy = service.sms_history('latest')
                    self.assertEqual([record['status'] for record in legacy['records'][:2]],
                                     ['pending', 'unknown'])
                    service.init_db(db)
                    service.init_db(db)
                    self.assertEqual(service.sms_history('latest'), legacy)
                    with db:
                        db.execute("UPDATE sms SET done=1, delivery_status='sent' WHERE hash='26'")
                        db.execute("UPDATE sms SET delivery_status='baseline' WHERE hash='1'")
                    page = service.sms_history('latest')
                    self.assertEqual(len(page['records']), 25)
                    self.assertEqual(page['records'][0]['status'], 'sent')
                    self.assertEqual(page['records'][0]['time'], '2026-11-21T20:28:03+08:00')
                    older = service.sms_history(page['next_cursor'])
                    self.assertEqual([record['body'] for record in older['records']], ['1', '0'])
                    self.assertEqual(older['records'][0]['status'], 'baseline')
                    self.assertIsNone(older['next_cursor'])
                    with self.assertRaises(ValueError):
                        service.sms_history('9223372036854775808')

    def test_history_time_zone_and_invalid_time(self):
        self.assertEqual(service.sms_time('621112028230a0'), '2026-11-21T20:28:03-00:30')
        self.assertIsNone(service.sms_time('00000000000000'))

    def test_modem_cleanup_keeps_newest_and_preserves_local_pending(self):
        messages = []
        with self.db:
            for i in range(18):
                stamp = bytes.fromhex('6211120282%02x23' % ((i % 10) * 16 + i // 10))
                pdu = deliver('短信 %d' % i, stamp=stamp)
                digest = hashlib.sha256(bytes.fromhex(pdu)).hexdigest()
                messages.append((i, pdu))
                self.db.execute('INSERT INTO sms VALUES (?, ?, ?, NULL, 1, 1, ?, ?, ?, ?)',
                                (digest, '1234', stamp.hex(), str(i), 0 if i == 1 else 1,
                                 None if i == 1 else 'sent', 9999999999))
        class FakeModem:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def list_sms_indexed(self): return list(messages)
            def delete_sms(self, index, expected_hash):
                item = next(item for item in messages if item[0] == index)
                self_hash = hashlib.sha256(bytes.fromhex(item[1])).hexdigest()
                if self_hash != expected_hash: raise RuntimeError('changed')
                messages.remove(item)
        with patch.object(service, 'Modem', FakeModem):
            service.cleanup_modem(self.db)
        self.assertEqual(len(messages), 15)
        self.assertNotIn(1, [item[0] for item in messages])
        self.assertEqual(self.db.execute('SELECT count(*) FROM sms WHERE done=0').fetchone()[0], 1)
        self.assertNotIn(0, [item[0] for item in messages])
        self.assertNotIn(2, [item[0] for item in messages])
        self.assertIn(3, [item[0] for item in messages])

    def test_modem_cleanup_keeps_incomplete_segment_in_database(self):
        stamp = bytes.fromhex('62111202823023')
        first = deliver('第一段', bytes([5, 0, 3, 42, 2, 1]), stamp)
        digest = hashlib.sha256(bytes.fromhex(first)).hexdigest()
        with self.db:
            self.db.execute('INSERT INTO sms VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                            (digest, '1234', stamp.hex(), 42, 2, 1, '第一段', 1, 'baseline', 9999999999))
        records = [(0, first)] + [(i, deliver(str(i), stamp=bytes.fromhex('62111212823023')))
                                   for i in range(1, 16)]
        deleted = []
        class FakeModem:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def list_sms_indexed(self): return records
            def delete_sms(self, index, expected_hash): deleted.append((index, expected_hash))
        with patch.object(service, 'Modem', FakeModem):
            service.cleanup_modem(self.db)
        self.assertEqual(deleted, [(0, digest)])
        self.assertEqual(self.db.execute('SELECT body FROM sms WHERE hash=?', (digest,)).fetchone()[0], '第一段')

    def test_modem_delete_checks_slot_before_deleting(self):
        pdu = deliver('短信')
        digest = hashlib.sha256(bytes.fromhex(pdu)).hexdigest()
        modem = service.Modem()
        with patch.object(modem, 'at', side_effect=['+CMGF: 0', '+CMGR: 1,,20\r\n' + pdu + '\r\nOK',
                                                    '+CMGF: 0', '+CMGR: 1,,20\r\n' + pdu + '\r\nOK', 'OK']) as at:
            with self.assertRaises(RuntimeError):
                modem.delete_sms(4, '0' * 64)
            self.assertNotIn('AT+CMGD=4', [call.args[0] for call in at.call_args_list])
            modem.delete_sms(4, digest)
            self.assertEqual(at.call_args_list[-1].args[0], 'AT+CMGD=4')

    def test_history_combines_multipart_across_pages(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(service, 'DB', str(pathlib.Path(directory) / 'state.sqlite3')):
            with closing(sqlite3.connect(service.DB)) as db:
                service.init_db(db)
                future = int(service.time.time()) + 3600
                with db:
                    db.execute('INSERT INTO sms VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                               ('part2', '123', '62111202823023', 42, 2, 2, '世界', 1, 'sent', future))
                    for i in range(30):
                        db.execute('INSERT INTO sms VALUES (?, ?, ?, NULL, 1, 1, ?, 1, ?, ?)',
                                   ('single%d' % i, '456', '62111202823023', str(i), 'sent', future))
                    db.execute('INSERT INTO sms VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                               ('part1', '123', '62111202824023', 42, 2, 1, '你好', 1, 'sent', future))
                    db.execute('INSERT INTO sms VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                               ('partial', '789', '62111202823023', 11, 2, 2, '后段', 0, None, future))
                first = service.sms_history('latest')
                self.assertEqual(first['records'][0]['body'], '［缺少第 1 段］后段')
                self.assertEqual(first['records'][0]['received_parts'], 1)
                self.assertEqual(first['records'][1]['body'], '你好世界')
                self.assertEqual(first['records'][1]['received_parts'], 2)
                second = service.sms_history(first['next_cursor'])
                self.assertEqual(len(first['records']) + len(second['records']), 32)
                self.assertFalse(any(r['body'] == '世界' for r in second['records']))

    def test_outgoing_pdu_and_confirmation(self):
        modem = service.Modem()
        modem.fd = 12
        replies = [b'> ', b'\r\n+CMGS: 42\r\n\r\nOK\r\n']
        with patch.object(modem, 'at'), patch.object(modem, 'read_until', side_effect=replies), \
             patch.object(service.termios, 'tcflush', create=True), \
             patch.object(service.termios, 'TCIFLUSH', 0, create=True), \
             patch.object(service.os, 'write') as write:
            modem.submit_sms('+12345', '测试')
        self.assertEqual(write.call_args_list[0].args[1], b'AT+CMGS=15\r')
        self.assertEqual(write.call_args_list[1].args[1], b'00110005912143F50008AA046D4B8BD5\x1a')

    def test_duplicate_submission_and_input_validation(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(service, 'OUTBOX', directory), \
             patch.object(service, 'DB', str(pathlib.Path(directory) / 'state.sqlite3')), \
             patch.object(service.subprocess, 'Popen') as launch:
            job = 'a' * 32
            service.submit_job(job, '12345', '5rWL6K+V')
            service.submit_job(job, '12345', '5rWL6K+V')
            launch.assert_called_once()
            self.assertEqual(service.job_status(job), {'status': 'queued', 'message': ''})
            service.update_job(str(pathlib.Path(directory) / job), 'sent', '模块已接受短信')
            records = service.outgoing_history('latest')['records']
            self.assertEqual([(r['number'], r['body'], r['status']) for r in records],
                             [('12345', '测试', 'sent')])
            pathlib.Path(directory, job).unlink()
            self.assertEqual(service.job_status(job)['status'], 'sent')
            service.submit_job(job, '12345', '5rWL6K+V')
            launch.assert_called_once()
            with self.assertRaises(ValueError):
                service.submit_job('b' * 32, '12;AT', '5rWL6K+V')

    def test_retention_cleans_pending_and_prevents_modem_reimport(self):
        first_read = datetime(2026, 11, 14, 12, 28, 3, tzinfo=timezone.utc).timestamp()
        cutoff = first_read + service.RETENTION_SECONDS
        expired = deliver('旧短信', stamp=bytes.fromhex('62114102823023'))
        unknown_time = deliver('时间无效')
        already_old = deliver('模块中的更旧短信', stamp=bytes.fromhex('62113102823023'))
        recent = deliver('新短信', stamp=bytes.fromhex('62111202823023'))
        with patch.object(service.time, 'time', return_value=first_read) as clock, \
             patch.object(service, 'Modem') as modem, patch.object(service, 'send') as send:
            list_sms = modem.return_value.__enter__.return_value.list_sms
            list_sms.return_value = [expired, unknown_time]
            service.poll(self.db, {})
            send.assert_not_called()
            self.assertEqual(self.db.execute('SELECT expires_at FROM sms').fetchall(), [(cutoff,), (cutoff,)])
            with self.db:
                self.db.execute("UPDATE sms SET done=0, delivery_status=NULL WHERE body='旧短信'")

            clock.return_value = cutoff - 1
            list_sms.side_effect = OSError('modem offline')
            with self.assertRaises(OSError):
                service.poll(self.db, {})
            self.assertEqual(self.db.execute('SELECT count(*) FROM sms').fetchone()[0], 2)

            clock.return_value = cutoff
            with self.assertRaises(OSError):
                service.poll(self.db, {})
            self.assertEqual(self.db.execute('SELECT count(*) FROM sms').fetchone()[0], 0)
            self.assertEqual(self.db.execute('SELECT count(*) FROM expired_sms').fetchone()[0], 2)

            list_sms.side_effect = None
            list_sms.return_value = [expired, unknown_time, already_old, recent]
            service.poll(self.db, {})
            service.poll(self.db, {})
            send.assert_called_once()
            self.assertIn('新短信', send.call_args.args[1])
            self.assertEqual(self.db.execute('SELECT body,delivery_status FROM sms').fetchall(),
                             [('新短信', 'sent')])

    def test_legacy_expiry_uses_sms_time(self):
        now = datetime(2026, 11, 21, 13, tzinfo=timezone.utc).timestamp()
        with closing(sqlite3.connect(':memory:')) as db, patch.object(service.time, 'time', return_value=now):
            with db:
                db.execute('CREATE TABLE sms (hash TEXT PRIMARY KEY, stamp TEXT, delivery_status TEXT)')
                db.executemany('INSERT INTO sms VALUES (?, ?, ?)', [
                    ('old', '62113102823023', 'sent'),
                    ('unknown', '00000000000000', 'baseline'),
                ])
            service.init_db(db)
            service.init_db(db)
            old, unknown = db.execute('SELECT expires_at,delivery_status FROM sms ORDER BY rowid').fetchall()
            self.assertLess(old[0], now)
            self.assertEqual(old[1], 'sent')
            self.assertEqual(unknown, (now + service.RETENTION_SECONDS, 'baseline'))

if __name__ == '__main__': unittest.main()
