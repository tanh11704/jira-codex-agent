import subprocess

from jira_codex_agent.verify.images import ImageReference, changed_image_references, is_declared


def git(root, *args):
    subprocess.run(['git', *args], cwd=root, check=True, capture_output=True)


def repo(tmp_path):
    git(tmp_path, 'init', '-q')
    git(tmp_path, 'config', 'user.email', 't@example.com')
    git(tmp_path, 'config', 'user.name', 't')
    page = tmp_path / 'apps/web/src/features/home/HomePage.tsx'
    page.parent.mkdir(parents=True)
    page.write_text('export const old = "/images/home/old.webp";\n')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'base')
    return page


def test_finds_images_in_added_lines_and_new_files_only(tmp_path):
    page = repo(tmp_path)
    page.write_text('export const old = "/images/home/old.webp";\n'
                    'const hero = `${base}/images/home/hero.webp`;\n'
                    '<input accept=".png,.jpg" />\n')
    assets = tmp_path / 'apps/web/src/features/home/assets.ts'
    assets.write_text("import art from './art/empty.svg';\nconst w = 'https://images.unsplash.com/photo-1.jpg?w=800';\n")
    (tmp_path / 'apps/web/src/features/home/HomePage.test.tsx').write_text('const f = "fixture.png";\n')
    (tmp_path / '.ui-evidence').mkdir()
    (tmp_path / '.ui-evidence/home-360.png').write_bytes(b'x')
    (tmp_path / 'apps/web/public/images/home').mkdir(parents=True)
    (tmp_path / 'apps/web/public/images/home/added.webp').write_bytes(b'x')

    found = {(item.file, item.line, item.reference) for item in changed_image_references(tmp_path)}
    assert found == {
        ('apps/web/src/features/home/HomePage.tsx', 2, '/images/home/hero.webp'),
        ('apps/web/src/features/home/assets.ts', 1, './art/empty.svg'),
        ('apps/web/src/features/home/assets.ts', 2, 'https://images.unsplash.com/photo-1.jpg'),
        ('apps/web/public/images/home/added.webp', 0, 'apps/web/public/images/home/added.webp'),
    }


def test_declared_slots_match_by_path_suffix_or_url():
    slots = [{'path': 'apps/web/public/images/home/hero.webp', 'url': ''},
             {'path': 'public/images/x.jpg', 'url': 'https://images.unsplash.com/photo-1.jpg'}]
    assert is_declared(ImageReference('a.tsx', 1, '/images/home/hero.webp'), slots)
    assert is_declared(ImageReference('a.tsx', 1, 'https://images.unsplash.com/photo-1.jpg'), slots)
    assert not is_declared(ImageReference('a.tsx', 1, '/images/home/ero.webp'), slots)
    assert not is_declared(ImageReference('a.tsx', 1, '/images/other/hero.webp'), slots)
    assert not is_declared(ImageReference('a.tsx', 1, 'https://example.com/hero.webp'), slots)
