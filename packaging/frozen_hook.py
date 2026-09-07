# PyInstaller runtime hook: set up paths so that the frozen binary can locate
# the bundled DSDL compiled types and source definitions.
#
# In a frozen build, sys._MEIPASS points to the temp extraction directory.
# The compiled DSDL types live at <MEIPASS>/python_compiled_messages/ and
# need to be on both sys.path and PYCYPHAL_PATH so pycyphal can import them.

import os
import sys
from pathlib import Path

_meipass = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))

compiled_dir = _meipass / "python_compiled_messages"
dsdl_dir = _meipass / "dsdl_messages"

if compiled_dir.is_dir():
    resolved = str(compiled_dir.resolve())
    if resolved not in sys.path:
        sys.path.insert(0, resolved)
    os.environ["PYCYPHAL_PATH"] = resolved

    public_types = dsdl_dir / "public_regulated_data_types"
    if public_types.is_dir():
        existing = os.environ.get("CYPHAL_PATH", "")
        if str(public_types.resolve()) not in existing:
            os.environ["CYPHAL_PATH"] = str(public_types.resolve()) + (
                os.pathsep + existing if existing else ""
            )
