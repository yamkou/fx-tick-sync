"""Operator-reviewed configuration, NEVER loaded from artifacts, UI or ledgers.

No real feed is approved. A future publication rollout must explicitly review
source scope AND attest each exact content hash / canonical lineage hash here
or in an equivalently protected configuration service. Legacy registration does
not write either collection. Empty configuration intentionally denies all feeds.
"""
SOURCE_POLICIES = ()
DISTRIBUTION_ATTESTATIONS = {}
