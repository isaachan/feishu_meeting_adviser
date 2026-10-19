"""Dispatch persisted replies directly during the demo phase."""
from .reply_sender import send_saved_reply


class ReplyDispatcher:
    def __init__(self, data, source, client, *, meeting_id: str, consumer_id: str,
                 settings, session: dict, clock, diagnose=None):
        self.data = data
        self.source = source
        self.client = client
        self.meeting_id = meeting_id
        self.consumer_id = consumer_id
        self.settings = settings
        self.session = session
        self.clock = clock
        self.diagnose = diagnose

    def discard_expired(self) -> int:
        return self.data.discard_expired_replies(
            self.meeting_id,
            self.settings,
            now=self.clock(),
            joined_at=self.session['joined_at'],
            bot_id=self.session.get('bot_user_id'),
        )

    def dispatch(self) -> bool:
        # DEMO: bypass source backlog and reply evidence validity checks.
        row = self.data.next_reply(self.meeting_id, now=self.clock())
        if row is None:
            return False
        return send_saved_reply(
            self.data,
            self.client,
            row,
            clock=self.clock,
            interval=self.settings.reply_interval_seconds,
            diagnose=self.diagnose,
        )
