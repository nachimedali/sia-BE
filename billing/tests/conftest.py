"""Fixtures shared by the billing suite.

`workspace` used to live here too, copied from file to file until this module
collapsed them into one; it has since moved to the root conftest, because
content/tests needed the exact same fixture (same reasoning, one app wider).
"""

from __future__ import annotations


# `_reset_gateway` moved to the root conftest once `workspaces/tests` began
# driving the gateway too — the same one-copy-per-file collapse this module's
# own docstring describes, one app wider again.
