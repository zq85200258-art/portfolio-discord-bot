"""Read and validate the HTTP proxy shared by Discord and RSS."""

import os
from dataclasses import dataclass, field

from aiohttp import BasicAuth
from yarl import URL


class ProxyConfigError(ValueError):
    """A configured proxy URL could not be used."""


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    url: str
    auth: BasicAuth | None = field(default=None, repr=False)


def load_proxy() -> ProxyConfig | None:
    """Prefer PROXY_URL, then HTTPS_PROXY, ignoring blank values."""
    for key in ("PROXY_URL", "HTTPS_PROXY"):
        value = os.environ.get(key, "").strip()
        if value:
            break
    else:
        return None

    try:
        proxy = URL(value)
        if (
            any(char.isspace() or ord(char) < 32 or ord(char) == 127 or char == "\\" for char in value)
            or proxy.scheme not in {"http", "https"}
            or not proxy.host
            or not proxy.port
            or proxy.path != "/"
            or proxy.query_string
            or proxy.fragment
        ):
            raise ValueError
        auth = BasicAuth.from_url(proxy)
        if auth is not None:
            auth.encode()
        return ProxyConfig(str(proxy.with_user(None)), auth)
    except ValueError:
        raise ProxyConfigError(f"Invalid {key}. Use an HTTP(S) proxy URL with a valid host and port.") from None
