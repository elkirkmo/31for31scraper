from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name):
    return (FIXTURES_DIR / name).read_text()
