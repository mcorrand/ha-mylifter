"""Constants for the MyLifter integration."""

from datetime import timedelta
from typing import Final

DOMAIN: Final = "mylifter"

CONF_PASSKEY: Final = "passkey"
CONF_ENTRY_TYPE: Final = "entry_type"
CONF_MULTI_MOTOR: Final = "multi_motor"
CONF_MOTOR_ARRANGEMENT: Final = "motor_arrangement"
CONF_MEMBERS: Final = "members"
CONF_TOP_POSITION: Final = "top_position"
CONF_BOTTOM_POSITION: Final = "bottom_position"
CONF_RESET_CALIBRATION: Final = "reset_calibration"

ENTRY_TYPE_MOTOR: Final = "motor"
ENTRY_TYPE_GROUP: Final = "group"

ARRANGEMENT_SINGLE: Final = "single"
ARRANGEMENT_MULTI: Final = "multi"

# Fixed (not user configurable) poll interval for status while idle.
POLL_INTERVAL: Final = timedelta(minutes=5)

# Raw position distance from the top smart point still treated as "closed" (stowed).
CLOSED_TOLERANCE: Final = 50

# Group safety: maximum difference in travel (raw ticks) between members of a group
# since the start of a move, and how long other members may keep moving after one
# member finished on its own.
GROUP_MAX_DIVERGENCE: Final = 300
GROUP_FINISH_GRACE: Final = 5.0

MANUFACTURER: Final = "GarageSmart / MyLifter"
MODEL: Final = "MyLifter motor"
