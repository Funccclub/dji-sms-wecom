import importlib.util
import pathlib
import sqlite3
import sys
import types
import unittest
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
        self.db.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)')
        self.db.execute('CREATE TABLE sms (hash TEXT PRIMARY KEY,sender TEXT,stamp TEXT,ref INTEGER,total INTEGER,part INTEGER,body TEXT,done INTEGER)')
        self.db.commit()
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
            FakeModem.messages = [old,p2]
            service.poll(self.db, {})
            send.assert_not_called()
            FakeModem.messages = [old,p2,p1]
            send.side_effect = RuntimeError('network')
            with self.assertRaises(RuntimeError): service.poll(self.db,{})
            send.side_effect = None
            service.poll(self.db,{})
            self.assertIn('你好世界', send.call_args.args[1])
            service.poll(self.db,{})
            self.assertEqual(send.call_count, 2)

if __name__ == '__main__': unittest.main()
