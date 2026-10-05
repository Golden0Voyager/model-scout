"""Which providers are currently switched on.

`ProviderConfig.default_enabled` is only the starting value; the live choice is stored
in the provider_settings table so that silencing a provider survives a restart and does
not require editing source code.

A provider absent from config has no default to fall back to and is reported disabled,
so callers can never probe something nobody declared.
"""

from core import database
from core.config import PROVIDERS, provider_default_enabled


async def effective_enabled() -> dict[str, bool]:
    """Every declared provider mapped to its current switch position."""
    stored = await database.get_provider_prefs()
    return {
        key: stored[key] if key in stored else provider_default_enabled(key)
        for key in PROVIDERS
    }


async def is_enabled(provider_key: str) -> bool:
    return (await effective_enabled()).get(provider_key, False)
