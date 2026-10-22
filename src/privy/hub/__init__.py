"""privy hub: a local control plane for many privy listeners reachable through Azure Relay.

The hub runs on an operator workstation, reads pre-hydrated Relay credentials
from ``relays.json``, and exposes the listeners behind them as *targets* through
a loopback REST API and a Model Context Protocol (MCP) endpoint.
"""
