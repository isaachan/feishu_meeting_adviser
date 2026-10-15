"""Fail-closed startup and identity checks for optional meeting materials."""
from .client import ApiError
from .materials import ContentPreviews, MaterialError
from .models import digest
from .oauth import OAuthError, OAuthManager
from .storage import StorageError


class MaterialBootstrapper:
    def __init__(self, data, config, session, *, client, loader, preview_display,
                 stop_event, clock, maintenance_lock, guard,
                 preview_factory=ContentPreviews):
        self.data = data
        self.config = config
        self.session = session
        self.client = client
        self.loader = loader
        self.preview_display = preview_display
        self.stop_event = stop_event
        self.clock = clock
        self.maintenance_lock = maintenance_lock
        self.guard = guard
        self.preview_factory = preview_factory
        self.oauth = getattr(client, 'oauth_manager', None)
        self.loaded = False
        self.subject = None

    def ensure(self) -> None:
        if self.loaded:
            return
        meeting_id = self.session['meeting_id']
        with self.maintenance_lock():
            self.guard()
            self.data.ensure_session(self.session)
            if not self.config.meeting_context.enabled:
                self.data.set_materials(
                    meeting_id,
                    {'state': 'disabled', 'context_version': None, 'documents': []},
                    now=self.clock(),
                )
                self.loaded = True
                return
            self.data.mark_material_loading(meeting_id)
        settings = self.config.meeting_context
        logger = self.preview_factory(
            self.config.data_directory / 'content_previews',
            meeting_id,
            max_bytes=settings.preview_bytes,
            backups=settings.preview_backups,
            display=self.preview_display,
        )
        try:
            snapshot = self.loader(
                self.config,
                self.session,
                client=self.client,
                emit=logger.write,
                stop_event=self.stop_event,
                clock=self.clock,
            )
        except (ApiError, OAuthError, MaterialError, StorageError,
                OSError, ValueError, TypeError, KeyError) as exc:
            reason = (exc.kind.value if isinstance(exc, ApiError)
                      else exc.reason if isinstance(exc, OAuthError)
                      else 'material_load_failed')
            with self.maintenance_lock():
                self.data.set_materials(
                    meeting_id,
                    {
                        'state': 'unavailable',
                        'context_version': digest([
                            'unavailable', settings.calendar_id, settings.event_id]),
                        'reason': reason,
                        'documents': [],
                    },
                    now=self.clock(),
                )
            logger.write({
                'kind': 'load',
                'status': 'unavailable',
                'reason': reason,
                'http_status': getattr(exc, 'http_status', None),
                'code': getattr(exc, 'code', None),
            })
            raise MaterialError(
                'meeting materials unavailable; collect continues independently; ' + reason) from None
        with self.maintenance_lock():
            self.guard()
            self.data.set_materials(meeting_id, snapshot, now=self.clock())
        self.subject = snapshot.get('subject_hash')
        self.loaded = True

    def verify_identity(self) -> None:
        if not self.loaded or self.subject is None:
            return
        try:
            oauth = self.oauth or OAuthManager(
                self.config.credentials, self.config.data_directory)
            subject = oauth.subject_hash()
        except OAuthError:
            raise MaterialError(
                'material user authorization unavailable; restart after authorizing') from None
        if subject != self.subject:
            raise MaterialError('material user account changed; restart consume')
