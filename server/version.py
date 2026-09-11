"""Single source of truth for the release version.

The desktop shell used to hold this in its Cargo manifest. With the shell
gone, the server binary is the product, so the version lives here. The
release workflow greps this file and refuses to build a tag that disagrees.
"""

__version__ = "0.7.0"
