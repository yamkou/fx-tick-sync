"""Strict adapter selection with logical secret references only, never values."""
from dataclasses import dataclass
from ..config import ConfigError, logical_id
from ..collectors.monitoring import Channel, NotificationRoute
from .providers import GenericWebhookProvider, LoggingNotificationProvider, FakeNotificationProvider


@dataclass(frozen=True)
class DeliveryConfig:
    route: NotificationRoute
    provider: str
    endpoint_reference: str | None = None
    token_reference: str | None = None

    def __post_init__(self):
        if not isinstance(self.route, NotificationRoute) or self.provider not in ('logging', 'fake', 'webhook'):
            raise ConfigError('Unsupported notification adapter')
        for value in (self.endpoint_reference, self.token_reference):
            if value is not None:
                logical_id(value)
        if self.provider == 'webhook':
            if self.endpoint_reference is None:
                raise ConfigError('Webhook requires a destination reference')
        elif self.endpoint_reference is not None or self.token_reference is not None:
            raise ConfigError('Local providers do not accept destination references')

    @classmethod
    def from_dict(cls, data):
        try:
            if not isinstance(data, dict) or set(data) != {'schema_version', 'route_id', 'channel', 'provider', 'endpoint_reference', 'token_reference'}:
                raise ValueError()
            if type(data['schema_version']) is not int or data['schema_version'] != 1:
                raise ValueError()
            return cls(NotificationRoute(data['route_id'], Channel(data['channel'])), data['provider'],
                data['endpoint_reference'], data['token_reference'])
        except (ValueError, TypeError):
            raise ConfigError('Invalid notification configuration; values omitted') from None

    def build(self, secrets=None, poster=None):
        if self.provider == 'logging':
            return LoggingNotificationProvider()
        if self.provider == 'fake':
            return FakeNotificationProvider()
        if secrets is None:
            raise ConfigError('Webhook needs a separately provisioned SecretProvider')
        return GenericWebhookProvider(self.endpoint_reference, secrets, self.token_reference, poster)
