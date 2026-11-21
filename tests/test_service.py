import importlib.util
import pathlib
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import closing
from unittest.mock import patch

if sys.platform == 'win32':
    sys.modules.setdefault('termios', types.ModuleType('termios'))
    sys.modules.setdefault('tty', types.ModuleType('tty'))

spec = importlib.util.spec_from_file_location('service', pathlib.Path(__file__).parents[1] / 'files' / 'dji-sms-wecom.py')
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)

def deliver(text, header=b''):
    body = header + text.encode('utf-16-be')
    return (bytes([0, 0x40 if header else 0, 4, 0x91, 0x21, 0x43, 0, 8]) + bytes(7) + bytes([len(body)]) + body).hex()

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

if __name__ == '__main__': unittest.main()
