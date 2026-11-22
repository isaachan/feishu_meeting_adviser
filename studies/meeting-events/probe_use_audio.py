"""Compare meeting.ability.use_audio with a host's manual microphone toggles.

This is an experiment, not a host microphone state API. The documented field
belongs to the meeting's ability object, rather than to a participant.
"""

import argparse
from datetime import datetime, timezone
import json
import os
import sys
import time
from urllib import error, parse, request

from poll import API, diagnostic
from poll_user import active_ids, authorize


PHASES = (('muted_1', '主持人静音'), ('unmuted', '主持人开麦'),
          ('muted_2', '主持人再次静音'))

def fetch_full(url, token):
    """Keep the complete JSON envelope returned by Feishu."""
    req = request.Request(url, headers={
        'Accept': 'application/json', 'Authorization': 'Bearer ' + token})
    try:
        with request.urlopen(req, timeout=15) as response:
            result = json.load(response)
            status = response.status
    except error.HTTPError as exc:
        try:
            result = json.load(exc)
        except (ValueError, OSError):
            result = None
        raise diagnostic('获取会议详情', exc.code, result) from None
    except (error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(f'获取会议详情请求失败：{type(exc).__name__}') from None
    if not isinstance(result, dict) or result.get('code') != 0:
        raise diagnostic('获取会议详情', status, result)
    return result


def details(meeting_id, token):
    query = parse.urlencode({'with_meeting_ability': 'true',
                             'with_participants': 'true'})
    result = fetch_full(API + '/vc/v1/meetings/' + parse.quote(meeting_id, safe='') + '?' + query,
                        token)
    data = result.get('data')
    meeting = data.get('meeting') if isinstance(data, dict) else None
    if not isinstance(meeting, dict):
        raise RuntimeError('会议详情缺少 meeting')
    return result


def meeting_from(result):
    return result['data']['meeting']


def resolve_active_meeting(meeting_no, token):
    matches = []
    for meeting_id in dict.fromkeys(active_ids(meeting_no, token)):
        result = details(meeting_id, token)
        meeting = meeting_from(result)
        if str(meeting.get('meeting_no')) == meeting_no and meeting.get('status') == 2:
            matches.append((meeting_id, result))
    if len(matches) != 1:
        raise RuntimeError(f'授权用户正在参加的会议中，会议号 {meeting_no} 对应 {len(matches)} 场进行中的会议；'
                           '需恰好一场，且授权用户本人须在会中')
    return matches[0]


def observation(meeting):
    ability = meeting.get('ability')
    return {
        'meeting_status': meeting.get('status'),
        'use_audio_present': isinstance(ability, dict) and 'use_audio' in ability,
        'use_audio': ability.get('use_audio') if isinstance(ability, dict) else None,
    }


def print_response(phase, sample, result):
    print(json.dumps({'observed_at': datetime.now(timezone.utc).isoformat(),
                      'phase': phase, 'sample': sample, 'response': result},
                     ensure_ascii=False), flush=True)


def assess(phases):
    values = []
    for phase in phases:
        if any(row['meeting_status'] != 2 for row in phase):
            return '无法判断：采样期间会议并非始终处于进行中。'
        if not phase or any(not row['use_audio_present'] or
                            type(row['use_audio']) is not bool for row in phase):
            return '无法判断：至少一次响应缺少布尔型 meeting.ability.use_audio。'
        observed = {row['use_audio'] for row in phase}
        if len(observed) != 1:
            return '无法判断：同一操作阶段内 use_audio 不稳定。'
        values.append(observed.pop())
    if values[0] == values[1] == values[2]:
        return 'use_audio 未随主持人静音/开麦切换；不能用它判断主持人的麦克风开关。'
    if values == [False, True, False]:
        return ('use_audio 与本次操作同步变化，但它是会议级字段；'
                '仍不能据此断定它表示主持人个人的麦克风状态。')
    return 'use_audio 的变化与静音/开麦顺序不符；不能用它判断主持人的麦克风开关。'


def main():
    parser = argparse.ArgumentParser(description='用用户 OAuth 对照主持人麦克风切换与会议 use_audio 字段')
    parser.add_argument('meeting_no', help='进行中的 9 位会议号')
    parser.add_argument('--meeting-id', help='已知会议 ID 时跳过会议号查询')
    parser.add_argument('--port', type=int, default=8767, help='本机 OAuth 回调端口，默认 8767')
    parser.add_argument('--once', action='store_true', help='只打印一次完整响应，不进入切换实验')
    parser.add_argument('--dump', action='store_true',
                        help='额外打印 user_active_meeting 的完整响应，用于核对字段形态')
    parser.add_argument('--samples', type=int, default=2, help='每阶段采样次数，默认 2')
    parser.add_argument('--interval', type=float, default=2.0, help='阶段内采样间隔秒数，默认 2')
    args = parser.parse_args()
    if len(args.meeting_no) != 9 or not args.meeting_no.isascii() or not args.meeting_no.isdecimal():
        parser.error('meeting_no 须为 9 位数字')
    if args.samples < 1 or args.interval < 2:
        parser.error('--samples 须 >= 1，--interval 须 >= 2 秒')
    if not 1024 <= args.port <= 65535:
        parser.error('--port 须在 1024..65535')
    app_id, app_secret = os.getenv('BOT_APP_ID'), os.getenv('BOT_APP_SECRET')
    token = os.getenv('FEISHU_USER_ACCESS_TOKEN')
    if not token and (not app_id or not app_secret):
        parser.error('请设置 BOT_APP_ID 和 BOT_APP_SECRET 以进行 OAuth，或提供 FEISHU_USER_ACCESS_TOKEN')

    try:
        token = token or authorize(app_id, app_secret, args.port)
        if args.dump:
            print_response('user_active_meeting', 0,
                           fetch_full(API + '/vc/v1/bots/user_active_meeting', token))
        if args.meeting_id:
            meeting_id = args.meeting_id
            result = details(meeting_id, token)
        else:
            meeting_id, result = resolve_active_meeting(args.meeting_no, token)
        meeting = meeting_from(result)
        if (str(meeting.get('meeting_no')) != args.meeting_no or
                meeting.get('status') != 2):
            raise RuntimeError('指定会议 ID 与会议号不匹配，或会议未在进行中')
        preflight = observation(meeting)
        print_response('preflight', 0, result)
        if args.once:
            return 0
        if not preflight['use_audio_present']:
            print('当前响应缺少 meeting.ability.use_audio；继续采集各阶段的完整响应。',
                  file=sys.stderr, flush=True)
        print(f'已定位进行中的会议 {meeting_id}。请由同一位主持人操作麦克风。',
              file=sys.stderr, flush=True)
        phases = []
        for phase, prompt in PHASES:
            input(f'{prompt}后按回车开始采样（Ctrl+C 退出）：')
            rows = []
            for sample in range(1, args.samples + 1):
                result = details(meeting_id, token)
                row = observation(meeting_from(result))
                rows.append(row)
                print_response(phase, sample, result)
                if sample < args.samples:
                    time.sleep(args.interval)
            phases.append(rows)
        print('结论：' + assess(phases), file=sys.stderr, flush=True)
    except (RuntimeError, EOFError) as exc:
        print(f'探针中止：{exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\n探针中止。', file=sys.stderr)
        return 130
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
