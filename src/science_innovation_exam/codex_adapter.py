"""Codex request adapter.

The framework gateway records metadata and forwards bytes. This module only
repairs a request shape that the current Codex build sends and that strict
relays reject: a namespace tool with a blank description. It does not read or
store the scientific text.
"""

FALLBACK_NAMESPACE_DESCRIPTION = "Tools provided by the Codex runtime."


def adapt(document):
    """Return ``(document, patch_count)``. ``document`` is mutated in place."""
    patched = 0
    inputs = document.get("input")
    if not isinstance(inputs, list):
        return document, patched
    for item in inputs:
        if not isinstance(item, dict):
            continue
        tools = item.get("tools")
        if not isinstance(tools, list):
            continue
        for tool in tools:
            if not isinstance(tool, dict) or tool.get("type") != "namespace":
                continue
            description = tool.get("description")
            if isinstance(description, str) and not description.strip():
                tool["description"] = FALLBACK_NAMESPACE_DESCRIPTION
                patched += 1
    return document, patched
