"""AgentWire's side of damage control: its profile, rules layer, CLI helpers and doctor checks.

Importing anything under ``agentwire.safety`` makes this process judge by AgentWire's profile.
"""

from agentwire.safety import host_profile

host_profile.activate()
