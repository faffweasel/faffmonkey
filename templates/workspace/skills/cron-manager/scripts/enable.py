import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from toggle import set_enabled

if __name__ == "__main__":
    set_enabled(True)
