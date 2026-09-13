"""Integration test package.

Tests here go through the real production entry points: the upstream config
schema, ``AgentFactory``, the Aemeath bridge, the patched upstream conversation
and TTS path, and the client protocol. Only model providers, audio devices and
the capture backend are substituted.
"""
