"""Pydantic validators for externalized config files.

Each schema mirrors one JSON file under config/{domain,linguistic,flow,per_tool}/.
Schemas are imported and applied at startup so any malformed config fails
before traffic reaches the bot.
"""
