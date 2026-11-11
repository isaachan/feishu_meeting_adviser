"""Real Feishu wiring: lark long connection + event dedup + sequential worker.

Ported from the legacy chatbot bot.py wiring pattern (lark.ws.Client, message
message_id dedup, one sequential worker thread); the chat backend is replaced
by BotService against the advisor API.
"""
import json
import logging
import threading
from queue import Full, Queue
from uuid import NAMESPACE_URL, uuid5

import lark_oapi as lark

from .service import BotService

LOG = logging.getLogger(__name__)


class LarkTransport:
    def __init__(self, app_id, app_secret):
        self.api = (lark.Client.builder().app_id(app_id)
                    .app_secret(app_secret).build())

    def send_dm(self, open_id, text) -> str:
        body = (lark.im.v1.CreateMessageRequestBody.builder()
                .receive_id(open_id).msg_type('text')
                .content(json.dumps({'text': text}, ensure_ascii=False)).build())
        request = (lark.im.v1.CreateMessageRequest.builder()
                   .receive_id_type('open_id')
                   .request_body(body).build())
        response = self.api.im.v1.message.create(request)
        if not response.success():
            raise RuntimeError(f'lark create failed: {response.code()}')
        return response.data().message_id

    def send_reply(self, message_id, text) -> str:
        uuid = str(uuid5(NAMESPACE_URL, message_id + text))
        body = (lark.im.v1.ReplyMessageRequestBody.builder()
                .msg_type('text')
                .content(json.dumps({'text': text}, ensure_ascii=False))
                .uuid(uuid).build())
        request = (lark.im.v1.ReplyMessageRequest.builder()
                   .message_id(message_id).request_body(body).build())
        response = self.api.im.v1.message.reply(request)
        if not response.success():
            raise RuntimeError(f'lark reply failed: {response.code()}')
        return response.data().message_id


def run(service: BotService, app_id: str, app_secret: str, *, queue_size=100,
        history_turns=8):
    """Start the sequential worker and the lark long connection (blocking)."""
    transport = LarkTransport(app_id, app_secret)
    service.transport = transport
    queue: Queue = Queue(maxsize=queue_size)

    def worker():
        while True:
            message_id, open_id, name, text = queue.get()
            try:
                service.handle_message(open_id, text, message_id, sender_name=name)
            except Exception:
                LOG.exception('handling message %s failed', message_id)
            finally:
                queue.task_done()

    threading.Thread(target=worker, name='bot-worker', daemon=True).start()
    service.start_polling()

    def on_message(event):
        message = getattr(getattr(event, 'event', None), 'message', None)
        LOG.info('event received: type=%s chat=%s',
                 getattr(message, 'message_type', None),
                 getattr(message, 'chat_type', None))
        if message is None or message.message_type != 'text':
            return
        sender_type = getattr(getattr(event.event, 'sender', None), 'sender_type', None)
        if sender_type == 'app':
            return
        if message.chat_type == 'group' and not getattr(message, 'mentions', None):
            return
        open_id = (getattr(getattr(event.event, 'sender', None), 'sender_id', None)
                   and event.event.sender.sender_id.open_id)
        name = (getattr(getattr(event.event, 'sender', None), 'sender_id', None)
                and '') or ''
        try:
            body = json.loads(message.content or '{}')
        except (ValueError, TypeError):
            body = {}
        text = body.get('text', '') if isinstance(body, dict) else ''
        try:
            queue.put_nowait((message.message_id, open_id, name, text))
            LOG.info('queued %s from %s', message.message_id, open_id)
        except Full:
            LOG.warning('message queue full; dropping %s', message.message_id)

    handler = (lark.EventDispatcherHandler.builder('', '')
               .register_p2_im_message_receive_v1(on_message).build())
    client = lark.ws.Client(app_id, app_secret, event_handler=handler,
                            log_level=lark.LogLevel.INFO)
    LOG.info('会参谋 bot 已启动（长连接）')
    client.start()
