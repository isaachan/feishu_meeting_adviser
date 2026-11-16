import contextlib
import io
import unittest

from poll_user import parse_token_response


class ParseTokenResponseTest(unittest.TestCase):
    def test_returns_top_level_token(self):
        result = {'access_token': 'u-token', 'scope': 'vc:meeting.meetingevent:read other'}
        self.assertEqual(parse_token_response(result), 'u-token')

    def test_missing_token_raises_with_keys(self):
        with self.assertRaisesRegex(RuntimeError, 'access_token'):
            parse_token_response({'error': 'x'})

    def test_missing_scope_warns_but_returns_token(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            token = parse_token_response({'access_token': 'u-token', 'scope': 'other'})
        self.assertEqual(token, 'u-token')
        self.assertIn('vc:meeting.meetingevent:read', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
