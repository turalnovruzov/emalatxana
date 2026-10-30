import os
import re

__version__ = "2.24.2"


def _layout(code: str) -> tuple:
    """(root, running, bin) for the code tree at `code`.

    An installed tree is `<root>/versions/<X.Y.Z>` with `<root>/current` a
    link and `<root>/source.git` a folder; its `bin` goes through `current`,
    so hooks and launch files baked from it survive the next update deleting
    this folder. Anything else is a checkout, which keeps its own `bin`.
    `running` is the folder's name, which need not equal `__version__`.
    """
    match = re.fullmatch(r"(.+)/versions/(\d+\.\d+\.\d+)", code)
    if match:
        root, running = match.groups()
        if os.path.islink(os.path.join(root, "current")) and os.path.isdir(os.path.join(root, "source.git")):
            return root, running, os.path.join(root, "current", "bin", "emx")
    return None, None, os.path.join(code, "bin", "emx")


CODE = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
ROOT, RUNNING, BIN = _layout(CODE)
