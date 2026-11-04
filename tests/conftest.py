import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The plugin directory is a package (hyphenated repo name): load it under a fixed name.
spec = importlib.util.spec_from_file_location(
    "undo_effects", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
module = importlib.util.module_from_spec(spec)
sys.modules["undo_effects"] = module
spec.loader.exec_module(module)
