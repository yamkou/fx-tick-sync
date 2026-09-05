"""Explicit environment lookup; references only in ordinary configuration."""
import os
import re
from ..config import ConfigError, logical_id


class EnvironmentSecrets:
    def __init__(self, references):
        self.references = dict(references)
        for reference, variable in self.references.items():
            logical_id(reference)
            if not isinstance(variable, str) or not re.fullmatch('[A-Z][A-Z0-9_]{0,127}', variable):
                raise ConfigError('Invalid secret environment reference')

    def get(self, reference):
        try:
            value = os.environ[self.references[reference]]
            if not value:
                raise KeyError()
            return value
        except KeyError:
            raise ConfigError('Required secret is unavailable; reference/value omitted') from None
