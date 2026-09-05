"""Secret access contract only. No credentials are loaded at import or stored here."""
from typing import Protocol


class SecretProvider(Protocol):
    def get(self, reference: str) -> str:
        """Resolve an operator-owned logical reference, without logging its value."""
        ...
