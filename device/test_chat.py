# SPDX-License-Identifier: GPL-3.0-or-later
"""Keyboard, channel, and chat state tests, without network or radio."""
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import chat
import radar


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.data = SimpleNamespace(lock=threading.Lock(), settings={'configured':True,'channels':[{'name':'secret','private':True}]},
                                    status={'mode':'chat'}, online=True, messages=[])
        self.view = chat.Chat(self.data, radar)

    def test_keyboard_and_composer(self):
        for code in (16,33,30,35):
            self.view.handle(code,1,False)
        self.assertEqual(self.view.text,'qfah')
        self.view.handle(16,1,True)
        self.assertEqual(self.view.text,'qfahQ')
        for code in (91,90,93,26,81,27,39,80,71,70,92,2):
            self.view.handle(code,1,False)
        self.assertEqual(self.view.text,"qfahQ.,?!'@#:-_/1")
        self.view.handle(14,1,False)
        self.assertEqual(self.view.text[-1],'/')
        self.view.text = 'x'*140
        self.view.handle(30,1,False)
        self.assertEqual(len(self.view.text.encode()),140)
        self.assertTrue(self.view.handle(1,1,False))

    def test_channel_filter_and_send(self):
        self.view.handle(15,1,False)
        self.assertEqual(self.view.channels()[self.view.channel], 'secret')
        self.view.text = 'Hello, World!'
        with patch.object(chat,'post',return_value={'ok':True}) as post:
            self.view.handle(28,1,False)
            deadline=time.monotonic()+2
            while self.view.sending and time.monotonic()<deadline:
                time.sleep(.01)
            post.assert_called_once_with('/api/send',{'text':'Hello, World!','channel':1})
        self.assertEqual(self.view.text,'')

    def test_private_interlock_blocks_public(self):
        self.view.text = 'test'
        with patch.dict('os.environ', {'MESHCHAT_PRIVATE_ONLY':'secret'}), patch.object(chat,'post') as post:
            self.view.submit()
            post.assert_not_called()
            self.assertEqual(self.view.notice, 'Private-channel safety lock')
            self.view.channel = 1
            self.view.submit()
            deadline = time.monotonic()+2
            while self.view.sending and time.monotonic()<deadline:
                time.sleep(.01)
            post.assert_called_once_with('/api/send', {'text':'test', 'channel':1})

    def test_failed_send_keeps_draft(self):
        self.view.text='unsent'
        with patch.object(chat,'post',side_effect=RuntimeError('locked')):
            self.view.submit()
            deadline=time.monotonic()+2
            while self.view.sending and time.monotonic()<deadline:
                time.sleep(.01)
        self.assertEqual(self.view.text,'unsent')
        self.assertIn('locked',self.view.notice)

    def test_sse_updates_existing_message(self):
        data = radar.Data.__new__(radar.Data)
        data.lock = threading.Lock()
        data.messages = [{'id':7,'text':'hello','status':'sent','repeats':0}]
        data.on_event('message', {'id':7,'text':'hello','status':'repeated','repeats':2})
        self.assertEqual(len(data.messages),1)
        self.assertEqual(data.messages[0]['repeats'],2)

    def test_cached_draw_updates_on_message_and_key(self):
        self.view.pending_mode = True
        self.view.notice = 'Radio restarting…'
        first = self.view.draw('http://localhost:8080')
        self.assertEqual(self.view.notice, '')
        self.assertIs(first, self.view.draw('http://localhost:8080'))
        self.view.handle(30,1,False)
        second = self.view.draw('http://localhost:8080')
        self.assertIsNot(first,second)
        self.data.messages.append({'id':1,'channel':'Public','text':'hi','src':'sender','ts':time.time()})
        self.assertIsNot(second, self.view.draw('http://localhost:8080'))

    def test_setup_scan_and_render(self):
        self.data.settings={'configured':False}
        self.assertEqual(self.view.draw('http://127.0.0.1:8080').size,(320,170))
        self.view.handle(28,1,False)
        self.assertFalse(self.view.pending_mode)
        self.data.settings={'configured':True,'channels':[]}
        self.data.status={'mode':'scan'}
        with patch.object(chat,'post',return_value={'ok':True}) as post:
            self.view.handle(28,1,False)
            deadline=time.monotonic()+2
            while not post.called and time.monotonic()<deadline:
                time.sleep(.01)
            post.assert_called_once_with('/api/mode',{'mode':'chat'})
        self.assertEqual(self.view.draw('http://127.0.0.1:8080').size,(320,170))


if __name__=='__main__':
    unittest.main()
