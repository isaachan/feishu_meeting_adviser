import unittest
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

from poll import fetch, observations


class ObservationsTest(unittest.TestCase):
    def test_meeting_lifecycle_and_participant_changes(self):
        waiting = {'status': 1, 'participants': []}
        started = {'status': 2, 'participants': [{'id': 'host', 'status': 2}]}
        joined = {'status': 2, 'participants': [
            {'id': 'host', 'status': 2}, {'id': 'phone', 'status': 2}]}
        left = {'status': 2, 'participants': [
            {'id': 'host', 'status': 2}, {'id': 'phone', 'status': 4}]}
        ended = {'status': 3, 'participants': [
            {'id': 'host', 'status': 4}, {'id': 'phone', 'status': 4}]}

        self.assertEqual(list(observations(waiting, started)),
                         [('会议开始', None), ('人加入', 'host')])
        self.assertEqual(list(observations(started, joined)), [('人加入', 'phone')])
        self.assertEqual(list(observations(joined, left)), [('人退出', 'phone')])
        self.assertEqual(list(observations(left, ended)),
                         [('人退出', 'host'), ('会议结束', None)])
        self.assertEqual(list(observations(ended, ended)), [])

    def test_end_when_participants_disappear(self):
        in_progress = {'status': 2, 'participants': [{'id': 'host', 'status': 2}]}
        self.assertEqual(list(observations(in_progress, {'status': 3})),
                         [('人退出', 'host'), ('会议结束', None)])

    def test_http_error_exposes_api_code_without_token(self):
        response = BytesIO(b'{"code":121003,"msg":"param error"}')
        failure = HTTPError('https://open.feishu.cn', 400, 'Bad Request', {}, response)
        with patch('poll.request.urlopen', side_effect=failure):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 400，API code=121003') as caught:
                fetch('https://open.feishu.cn/open-apis/vc/v1/meetings/list_by_no',
                      operation='按会议号查询', token='secret-token')
        self.assertIn('param error', str(caught.exception))
        self.assertNotIn('secret-token', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
