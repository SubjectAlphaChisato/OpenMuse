"""OpenMuse: an open-source, self-hostable personal agent. The Python package keeps its original name, opa."""
from importlib.metadata import PackageNotFoundError, version as _version

try:
    __version__ = _version("opa")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0+source"
