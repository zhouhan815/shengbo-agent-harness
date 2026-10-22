import unittest

from tools.douyin_live_probe import room_id, normalize, integer


class DouyinProbeTests(unittest.TestCase):
    def test_only_public_douyin_room_identifiers_are_accepted(self):
        self.assertEqual(room_id('https://live.douyin.com/505233570304'),'505233570304')
        self.assertEqual(room_id('505233570304'),'505233570304')
        for url in ('https://127.0.0.1/123456','https://evil.example/123456','https://user:pass@live.douyin.com/123456','https://live.douyin.com:8765/123456'):
            with self.assertRaises(ValueError): room_id(url)

    def test_unknown_or_display_count_is_not_zero(self):
        self.assertIsNone(integer(None))
        self.assertIsNone(integer('1.2万'))
        self.assertIsNone(integer(True))
        self.assertEqual(integer('4201'),4201)

    def test_comment_has_platform_room_and_stable_event_id(self):
        x=normalize({'method':'WebcastChatMessage','common':{'msgId':'7696750821586687039'},'user':{'id':'abc','nickname':'昵称'},'content':'哪里发货'},'505233570304')
        self.assertEqual(x['type'],'comment'); self.assertEqual(x['text'],'哪里发货')
        self.assertEqual(x['external_room_id'],'505233570304')
        self.assertEqual(x['event_id'],'7696750821586687039')
        self.assertEqual(x['source'],'real_external_probe')
        self.assertNotIn('nickname',x); self.assertNotIn('abc',str(x))

    def test_metrics_keep_online_and_cumulative_separate(self):
        x=normalize({'method':'WebcastRoomUserSeqMessage','total':'4201','totalUser':'1246837','totalPvForAnchor':'124.7万','onlineUserForAnchor':'4201'},'r')
        self.assertEqual(x['online_total_candidate'],4201)
        self.assertEqual(x['total_user_raw'],'1246837')
        self.assertEqual(x['total_pv_for_anchor_raw'],'124.7万')
        self.assertIn('verify',x['metric_basis'])

    def test_like_messages_keep_batch_and_total_separate(self):
        x=normalize({'method':'WebcastLikeMessage','count':'3','total':'100'},'r')
        self.assertEqual(x['count'],3);self.assertEqual(x['total'],100)

    def test_entry_does_not_invent_an_online_count(self):
        x=normalize({'method':'WebcastMemberMessage','memberCount':'200'},'r')
        self.assertEqual(x['type'],'enter');self.assertNotIn('online_total_candidate',x)

    def test_unknown_message_does_not_become_a_comment(self):
        self.assertIsNone(normalize({'method':'WebcastGiftMessage','content':'gift'},'r'))


if __name__=='__main__':unittest.main()
