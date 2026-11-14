"""Shared test helpers. Runs before any _helpers submodule, so its snapshot predates their imports."""
import os as _os

# The process environment before any helper could replace it; hermetic_env refuses any other object.
ORIGINAL_ENVIRON = _os.environ
