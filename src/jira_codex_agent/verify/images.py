"""Find images the agent's change uses, so slots it forgot to list in its report still reach the human."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

IMAGE_EXTENSIONS = ('png', 'jpg', 'jpeg', 'webp', 'gif', 'svg', 'avif')
# Files whose text can load an image; JSON, Markdown and lockfiles are left out to avoid noise.
SOURCE_EXTENSIONS = {'.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.vue', '.svelte', '.astro', '.html',
                     '.css', '.scss', '.sass', '.less'}
# A path or URL ending in an image extension; ${...} and quotes end the match, so template prefixes drop off.
IMAGE_REFERENCE = re.compile(r'(?:https?://)?[\w./@%+~-]*\.(?:' + '|'.join(IMAGE_EXTENSIONS) + r')(?![\w])',
                             re.IGNORECASE)
IGNORED_PARTS = {'.ui-evidence', 'node_modules', 'dist', 'build', 'coverage', '__tests__', '__snapshots__'}
HUNK = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@')
MAX_SOURCE_BYTES = 1_000_000


@dataclass(frozen=True)
class ImageReference:
    file: str  # worktree-relative file that uses the image, or the image file itself when it was added
    line: int  # 0 for an added image file
    reference: str  # the path or URL as written


def _ignored(path: str) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1] if parts else ''
    return bool(IGNORED_PARTS.intersection(parts)) or '.test.' in name or '.spec.' in name or '.stories.' in name


def _is_source(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in SOURCE_EXTENSIONS and not _ignored(path)


def _is_image(path: str) -> bool:
    return PurePosixPath(path).suffix.lower().lstrip('.') in IMAGE_EXTENSIONS and not _ignored(path)


def _git(root: Path, *args: str) -> str | None:
    try:
        return subprocess.run(['git', *args], cwd=root, capture_output=True, text=True, timeout=60, check=True,
                              errors='replace').stdout
    except (OSError, subprocess.SubprocessError):
        return None


def _references(text: str) -> list[str]:
    # A bare extension (accept=".png,.jpg") names a file type, not an image the screen shows.
    return [match.group(0) for match in IMAGE_REFERENCE.finditer(text) if PurePosixPath(match.group(0)).suffix]


def changed_image_references(root: Path) -> list[ImageReference]:
    """Image paths in lines the uncommitted change adds, plus image files it adds, in the worktree at root."""
    found: list[ImageReference] = []
    seen: set[tuple[str, str]] = set()

    def add(file: str, line: int, reference: str) -> None:
        if (file, reference) not in seen:
            seen.add((file, reference))
            found.append(ImageReference(file, line, reference))

    # Tracked files: only the added lines, so images the screen already had are not reported again.
    diff = _git(root, 'diff', 'HEAD', '-U0', '--no-color', '--no-ext-diff', '--diff-filter=AM') or ''
    current, number = None, 0
    for line in diff.splitlines():
        if line.startswith('+++ '):
            name = line[4:]
            current = name[2:] if name.startswith('b/') else None
        elif (hunk := HUNK.match(line)):
            number = int(hunk.group(1))
        elif current and line.startswith('+'):
            if _is_source(current):
                for reference in _references(line[1:]):
                    add(current, number, reference)
            number += 1
    for name in (_git(root, 'diff', 'HEAD', '--name-only', '--diff-filter=A') or '').splitlines():
        if _is_image(name):
            add(name, 0, name)

    # Untracked files are new in their entirety.
    for name in (_git(root, 'ls-files', '--others', '--exclude-standard') or '').splitlines():
        if _is_image(name):
            add(name, 0, name)
        elif _is_source(name):
            path = root / name
            try:
                if path.stat().st_size > MAX_SOURCE_BYTES:
                    continue
                text = path.read_text(errors='replace')
            except OSError:
                continue
            for number, content in enumerate(text.splitlines(), start=1):
                for reference in _references(content):
                    add(name, number, reference)
    return found


def _normalized(path: str) -> str:
    path = path.strip().split('?', 1)[0].split('#', 1)[0]
    while path.startswith(('./', '../')):
        path = path.split('/', 1)[1]
    return path.lstrip('/')


def _same_path(a: str, b: str) -> bool:
    """True when one path ends with the other on a segment boundary (served /images/x vs public/images/x)."""
    a, b = _normalized(a), _normalized(b)
    if not a or not b:
        return False
    shorter, longer = sorted((a, b), key=len)
    return longer == shorter or longer.endswith('/' + shorter)


def is_declared(reference: ImageReference, declared: list[dict]) -> bool:
    """Whether a reported image slot covers this reference, by URL for web images and by path otherwise."""
    value = reference.reference
    for slot in declared:
        if value.startswith(('http://', 'https://')):
            if value == (slot.get('url') or '').strip():
                return True
            continue
        for candidate in (slot.get('path'), slot.get('resolved_path')):
            if candidate and _same_path(value, str(candidate)):
                return True
    return False
