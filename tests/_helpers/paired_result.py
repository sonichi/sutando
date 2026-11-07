"""Publish successful result fixtures through the production receipt writer."""
import sys
from pathlib import Path


def publish_result(path, text, encoding=None, errors=None, newline=None):
    """Path.write_text-shaped fixture adapter; raw corruption tests keep write_text."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
    from result_publish import publish_text
    path = Path(path)
    target = path.with_name(path.name.split(".sending", 1)[0].removesuffix(".txt") + ".txt") if ".sending" in path.name else path
    publish_text(target, text, pair_result=True)
    if target != path:
        target.replace(path)
    return len(text)


def publish_staged_result(staged, path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
    from result_publish import publish_staged
    return publish_staged(staged, path, pair_result=True)
