"""AgentWire's side of damage control: its rules layer, CLI helpers and doctor checks.

Importing anything under ``agentwire.safety`` makes this process judge by AgentWire's profile
(``agentwire.safety_ext``).
"""

from agentwire import safety_ext

safety_ext.activate()
