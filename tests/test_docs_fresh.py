import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gen_docs", ROOT / "scripts" / "gen_docs.py")
gen_docs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen_docs)


def test_generated_reference_pages_are_current():
    stale = [
        name
        for name, text in gen_docs.render().items()
        if (gen_docs.OUT / name).read_text() != text
    ]
    assert not stale, f"stale: {stale}; run: uv run python scripts/gen_docs.py"
