"""Diagnostics for MyLifter."""

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import MyLifterConfigEntry
from .const import CONF_PASSKEY
from .group import MyLifterGroup

TO_REDACT = {CONF_PASSKEY}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: MyLifterConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data: dict[str, Any] = {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        }
    }
    runtime = entry.runtime_data
    if isinstance(runtime, MyLifterGroup):
        data["group"] = {
            "members": runtime.member_entry_ids,
            "members_loaded": runtime.try_members() is not None,
            "moving": runtime.moving.name if runtime.moving else None,
            "last_error": runtime.last_error,
        }
        return data
    data["state"] = asdict(runtime.client.state)
    data["session"] = runtime.client.diagnostics()
    data["last_update_success"] = runtime.last_update_success
    return data
