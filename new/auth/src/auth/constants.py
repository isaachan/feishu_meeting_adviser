"""Shared Feishu OAuth constants (ported from legacy recorder oauth.py)."""

AUTHORIZE_URL = 'https://accounts.feishu.cn/open-apis/authen/v1/authorize'
CODE_TOKEN_URL = 'https://open.feishu.cn/open-apis/authen/v2/oauth/token'
TOKEN_URL = 'https://accounts.feishu.cn/oauth/v3/token'
USER_INFO_URL = 'https://open.feishu.cn/open-apis/authen/v1/user_info'
CALLBACK_PATH = '/oauth/callback'
NSDP_SUBMIT_PATH = '/nsdp/submit'
NSDP_INTENT = 'nsdp:token'
GUI_LOGIN_INTENT = 'gui:login'
SCOPES = ('calendar:calendar:read', 'calendar:calendar.event:read', 'offline_access')
MAX_FILE_BYTES = 65_536
DEFAULT_STATE_TTL_S = 600
DEFAULT_PORT = 8381
