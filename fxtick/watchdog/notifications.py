"""Vendor-facing adapter interfaces. Transport is injected and tested offline.

No vendor endpoint is assumed/embedded here: a deployment-reviewed transport
owns current LINE Messaging API/SMTP specifics and credential handling.
"""
from dataclasses import dataclass
from typing import Protocol
from ..config import ConfigError, logical_id
from .messages import format_notification


class LineMessagingTransport(Protocol):
    def push_text(self, *, recipient: str, text: str, access_token: str, event_id: str) -> bool: ...


class EmailTransport(Protocol):
    def send_text(self, *, sender: str, recipient: str, subject: str, text: str,
                  credential: str, event_id: str) -> bool: ...


class LineMessagingProvider:
    """NotificationProvider adapter; LINE Messaging API, never LINE Notify."""
    def __init__(self, transport: LineMessagingTransport, secrets, token_reference, recipient_reference):
        logical_id(token_reference); logical_id(recipient_reference)
        self.transport,self.secrets=transport,secrets
        self.token_reference,self.recipient_reference=token_reference,recipient_reference

    def send(self,event,route):
        try:
            token=self.secrets.get(self.token_reference); recipient=self.secrets.get(self.recipient_reference)
            if not token or not recipient: return False
            return self.transport.push_text(recipient=recipient,text=format_notification(event),
                access_token=token,event_id=event.event_id) is True
        except Exception:
            return False


class EmailProvider:
    def __init__(self,transport: EmailTransport,secrets,sender_reference,recipient_reference,credential_reference):
        for value in (sender_reference,recipient_reference,credential_reference): logical_id(value)
        self.transport,self.secrets=transport,secrets
        self.sender_reference,self.recipient_reference,self.credential_reference=sender_reference,recipient_reference,credential_reference

    def send(self,event,route):
        try:
            sender=self.secrets.get(self.sender_reference); recipient=self.secrets.get(self.recipient_reference)
            credential=self.secrets.get(self.credential_reference)
            if not sender or not recipient or not credential or any(c in sender+recipient for c in '\r\n'): return False
            text=format_notification(event)
            return self.transport.send_text(sender=sender,recipient=recipient,subject=text.splitlines()[0],text=text,
                credential=credential,event_id=event.event_id) is True
        except Exception:
            return False
