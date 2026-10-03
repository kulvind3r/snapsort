"""Static + runtime data-safety contract verification."""
from __future__ import annotations
import ast
import pathlib
import pytest

SOURCE_FILES = ["organizer.py", "analyzer.py", "ui.py", "main.py", "journal.py"]
FORBIDDEN_CALLS = {"os.remove", "os.unlink", "shutil.rmtree"}


@pytest.mark.parametrize("source_file", SOURCE_FILES)
def test_no_destructive_calls(source_file):
    src = pathlib.Path(source_file).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                call = f"{node.value.id}.{node.attr}"
                assert call not in FORBIDDEN_CALLS, (
                    f"{source_file} line {node.lineno}: forbidden call '{call}'"
                )


def test_discarded_not_reprocessed(tmp_path, make_jpeg_file):
    from organizer import organize_directory

    make_jpeg_file("photo.jpg", date_str="2024:06:01 10:00:00")
    organize_directory(str(tmp_path))

    discarded = tmp_path / "_Discarded" / "2024-06-01" / "photo.jpg"
    discarded.parent.mkdir(parents=True, exist_ok=True)
    discarded.write_bytes(b"\xff\xd8" + b"\x00" * 50)

    organize_directory(str(tmp_path))

    assert discarded.exists(), "_Discarded file was moved on re-run"