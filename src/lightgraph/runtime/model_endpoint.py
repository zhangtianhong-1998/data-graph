"""Normalize user-facing Chat Completions settings without provider I/O."""
from urllib.parse import urlsplit, urlunsplit
import re


def normalize_model_base_url(value: str) -> str:
    """Accept a base or a full chat endpoint, retaining any custom proxy prefix."""
    value = value.strip()
    if not value:
        return value
    try:
        parsed = urlsplit(value)
        # Accessing port also validates malformed ports and IPv6 addresses.
        parsed.port
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment):
            raise ValueError
    except ValueError:
        raise ValueError("invalid_model_base_url") from None
    path = parsed.path.rstrip("/")
    suffix = "/chat/completions"
    if path.endswith(suffix):
        path = path[:-len(suffix)].rstrip("/")
    # Do not invent /v1: some gateways deliberately expose /chat/completions
    # at the origin. Repeated load/save must preserve the same endpoint.
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), path, "", ""))


def output_token_field(model: str) -> str:
    """Use the common gateway field; OpenAI reasoning models need the newer cap."""
    name = model.strip().lower().rsplit("/", 1)[-1]
    if re.match(r"^(?:o[134](?:-|$)|gpt-5(?:[.-]|$))", name):
        return "max_completion_tokens"
    return "max_tokens"
