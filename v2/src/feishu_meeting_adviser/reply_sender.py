"""Only the product sends meeting messages; persist uncertain outcomes for review."""
from .client import ApiError, ErrorKind, StopRequested
from .config import file_fingerprint
from .lifecycle import LifecycleError
from .models import digest

INTRODUCTION = ('大家好，我是会参谋，会结合讨论内容判断是否需要补充或回答。'
                '会议字幕会保存在本机，并发送给配置的 LLM 处理。'
                '配置日程材料时，还会读取日程描述及已授权文档，并发送给 LLM 作为会议背景。'
                '任何参会者都可要求暂停、恢复或取消发言；暂停发言时仍会处理字幕。'
                '如需完整停止采集和模型处理，请告知主持人。')


def send_saved_reply(data, client, row, *, clock, interval=0, diagnose=None):
    data.mark_sending(row['uuid'])
    try:
        client.send_message(row['meeting_id'],row['content'],row['uuid'])
    except ApiError as exc:
        uncertain = exc.kind not in (ErrorKind.DENIED,ErrorKind.PARAMETER,ErrorKind.TOKEN,ErrorKind.BOT_ABSENT,ErrorKind.RATE_LIMIT)
        data.mark_send_error(row['uuid'],exc.kind.value,uncertain=uncertain)
        if diagnose:
            diagnose({'kind':'reply','error_kind':exc.kind.value,'http_status':exc.http_status,'code':exc.code})
        return False
    except (StopRequested,OSError):
        data.mark_send_error(row['uuid'],'sender_interrupted',uncertain=True)
        return False
    data.mark_sent(row['uuid'],now=clock(),interval=interval)
    if diagnose:
        diagnose({'kind':'reply','reply_confirmed':True})
    return True


def introduce(data, manager, *, clock, diagnose=None):
    if file_fingerprint(manager.config.path)!=manager.config.fingerprint:
        raise LifecycleError('configuration changed; restart before introducing bot')
    session = manager.saved_meeting(require_joined=True)
    if manager.store.db.execute('SELECT 1 FROM tombstones WHERE identity=?',(digest(session['meeting_id']),)).fetchone():
        raise LifecycleError('meeting content was deleted; do not restart model consumption for this meeting')
    row = data.ensure_introduction(session,INTRODUCTION,now=clock())
    if row['state']=='sent':
        return True
    if row['state']!='pending':
        return False
    return send_saved_reply(data,manager.client,row,clock=clock,diagnose=diagnose)
