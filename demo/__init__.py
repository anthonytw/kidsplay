"""One-command demo, bundled sample media and README screenshot tooling.

Run from the repository root:

- ``just demo`` (``python -m demo``): start a throwaway server, seed it with
  the sample media in ``demo/media/`` and open the player in a window.
- ``just screenshots`` (``python -m demo.screenshots``): regenerate the
  images in ``docs/images/`` headlessly.
- ``python -m demo.make_sample_media``: regenerate ``demo/media/`` itself.
"""

import os

# pygame prints a banner on import, which lands in the middle of the demo's own
# output. Set before any submodule (or child process) imports it.
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
