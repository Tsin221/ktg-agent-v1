"""Optional server-created visitor scope; business user IDs remain unchanged."""
from collections.abc import Mapping


def identity_scope(identity: Mapping[str, object]) -> str:
    scope = identity.get("_agent_scope")
    return f":visitor:{scope}" if isinstance(scope, str) and scope else ""
