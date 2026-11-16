"""Probe Feishu's four meeting lifecycle push events over a long connection."""

import argparse
from datetime import datetime, timezone
import json
import os
import sys


EVENTS = {
    'vc.meeting.meeting_started_v1': '会议开始',
    'vc.meeting.meeting_ended_v1': '会议结束',
    'vc.meeting.join_meeting_v1': '人加入',
    'vc.meeting.leave_meeting_v1': '人退出',
}


def main():
    parser = argparse.ArgumentParser(description='监听飞书会议开始/结束和参会人进出事件')
    parser.add_argument('--meeting-no', help='仅显示指定 9 位会议号的事件')
    args = parser.parse_args()
    if args.meeting_no and (len(args.meeting_no) != 9 or not args.meeting_no.isascii()
                            or not args.meeting_no.isdecimal()):
        parser.error('--meeting-no 须为 9 位数字')

    app_id = os.environ.get('BOT_APP_ID')
    app_secret = os.environ.get('BOT_APP_SECRET')
    if not app_id or not app_secret:
        parser.error('请设置 BOT_APP_ID 和 BOT_APP_SECRET（同一个飞书应用）')

    try:
        import lark_oapi as lark
    except ImportError:
        parser.error('缺少 lark-oapi；参见 README.md 安装依赖')

    def receive(data):
        # Only print diagnostic metadata; SDK's full event contains user/tenant details.
        payload = json.loads(lark.JSON.marshal(data))
        header = payload.get('header') or {}
        event = payload.get('event') or {}
        meeting = event.get('meeting') or {}
        if args.meeting_no and str(meeting.get('meeting_no')) != args.meeting_no:
            return
        operator = event.get('operator') or {}
        identity = operator.get('id') or {}
        print(json.dumps({
            'received_at': datetime.now(timezone.utc).isoformat(),
            'event_type': header.get('event_type'),
            'label': EVENTS.get(header.get('event_type')),
            'event_id': header.get('event_id'),
            'meeting_id': meeting.get('id'),
            'meeting_no': meeting.get('meeting_no'),
            'meeting_source': meeting.get('meeting_source'),
            'operator_open_id': identity.get('open_id'),
            'leave_reason': event.get('leave_reason'),
        }, ensure_ascii=False), flush=True)

    handler = (lark.EventDispatcherHandler.builder('', '')
               .register_p2_vc_meeting_meeting_started_v1(receive)
               .register_p2_vc_meeting_meeting_ended_v1(receive)
               .register_p2_vc_meeting_join_meeting_v1(receive)
               .register_p2_vc_meeting_leave_meeting_v1(receive)
               .build())
    print('正在建立飞书长连接；仅已订阅、已发布且由 Open API 预约的会议会推送这四类事件。',
          file=sys.stderr, flush=True)
    lark.ws.Client(app_id, app_secret, event_handler=handler,
                   log_level=lark.LogLevel.INFO).start()


if __name__ == '__main__':
    main()
