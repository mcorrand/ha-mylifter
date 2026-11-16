"""Diagnostics."""

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mylifter.diagnostics import async_get_config_entry_diagnostics
from homeassistant.core import HomeAssistant

from .fake_device import PASSKEY


async def test_diagnostics_redacts_passkey(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The passkey never appears in diagnostics.

    Called directly: the HTTP-based helper needs a local port, which the
    development sandbox does not allow.
    """
    result = await async_get_config_entry_diagnostics(hass, init_integration)
    assert result["entry"]["data"]["passkey"] == "**REDACTED**"
    assert PASSKEY.hex() not in str(result)
    assert result["state"]["firmware"] == "3.1"
    assert result["state"]["weight"] == 42
    assert result["session"]["intent"] == "STOP"
    assert result["last_update_success"] is True
