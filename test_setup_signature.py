from pathlib import Path
import importlib.util

root = Path(__file__).resolve().parent
module_path = root / 'main.py'
spec = importlib.util.spec_from_file_location('main_module', module_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

print('main.py imported successfully')
