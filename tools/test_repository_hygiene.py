#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Hygiene guards for the files this repository is about to publish.

The checked set is the PUBLICATION set, not the whole working tree: with a
.git present it comes from `git ls-files --cached --others --exclude-standard`
so caches, virtualenvs and other gitignored runtime junk created by CI or
local runs stays out of the checks; while there is no .git yet, a minimal
.gitignore-aware walk approximates the same set.

Set SPACE_ARENA_NO_GIT=1 to use the working-tree walk even in a checkout;
this mode never invokes Git and does not inspect its publication index.

Guards:
  * no personal root-level journals/notes/prompt files get published;
  * no caches, virtualenvs or secret-named files get published;
  * no nested .git directories anywhere (vendor ships as plain source);
  * no legacy private host-project identifiers or personal absolute paths
    in non-vendor text sources.

Secret material is matched by file NAME only; file contents are never printed
here beyond short allowlisted filenames.  Vendor trees are third-party source
and are exempt from the text scans.  Forbidden identifiers and path patterns
below are assembled from fragments so that this file cannot match its own
patterns when scanned.
"""
import fnmatch
import os
import re
import subprocess
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_SCAN_BYTES = 512 * 1024

JOURNAL_NAMES = frozenset((
    'PROGRESS.md', 'NEXT.md', 'MODEL_NOTES.md', 'SESSION_NOTES.md',
    'PROMT.md', 'PROMPT.md',
))

CACHE_DIR_NAMES = frozenset((
    '__pycache__', '.venv', '.venvs', 'venv', '.pytest_cache', '.mypy_cache',
    '.ruff_cache', '.tox', '.nox', 'node_modules', 'htmlcov', '.cache',
))
CACHE_FILE_NAMES = frozenset(('.coverage', 'coverage.xml'))
CACHE_FILE_SUFFIXES = ('.pyc', '.pyo')

SECRET_NAME_GLOBS = (
    '*.key', '*.pem', '*.p12', '*.pfx', '*.p8',
    'id_rsa*', 'id_ed25519*', 'known_hosts', 'authorized_keys',
    'credentials*.json', 'secrets.json', 'secrets.yaml', 'secrets.yml',
    '*.kdbx',
)
ENV_ALLOWLIST = frozenset(('.env.example', '.env.template'))

# Fragments keep the guard file clean under its own scans.
LEGACY_TOKENS = ('ba' + 'jaj', 'fb' + 'a_')
_USER_SEGMENT = r'[A-Za-z0-9][A-Za-z0-9._-]{1,63}'
PRIVATE_PATH_PATTERNS = (
    re.compile(r'/' + 'home' + r'/' + _USER_SEGMENT),
    re.compile(r'/U' + 'sers' + r'/' + _USER_SEGMENT),
    re.compile(re.escape(':' + chr(92)) + 'Users'),
)


def _ignore_rules():
    gitignore = ROOT / '.gitignore'
    if not gitignore.is_file():
        return ()
    rules = []
    for line in gitignore.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        negate = line.startswith('!')
        if negate:
            line = line[1:]
        dirs_only = line.endswith('/')
        line = line.strip('/')
        if line:
            rules.append((negate, dirs_only, '/' in line,
                          re.compile(fnmatch.translate(line))))
    return tuple(rules)


RULES = _ignore_rules()


def _ignored(parts, is_dir=False):
    """Approximates gitignore matching well enough for this repo's rules:
    last matching rule wins; dir-only rules never match file basenames."""
    decision = False
    for negate, dirs_only, anchored, rx in RULES:
        if dirs_only:
            if anchored:
                hit = any(rx.fullmatch('/'.join(parts[:k]))
                          for k in range(1, len(parts) + (1 if is_dir else 0)))
            else:
                hit = any(rx.fullmatch(seg)
                          for seg in (parts if is_dir else parts[:-1]))
        elif anchored:
            hit = bool(rx.fullmatch('/'.join(parts)))
        else:
            hit = bool(rx.fullmatch(parts[-1]))
        if hit:
            decision = not negate
    return decision


def _published_files():
    # Allow source-tree audits without invoking Git (for example in archives
    # or when the repository owner handles all Git operations).
    if (ROOT / '.git').is_dir() and os.environ.get('SPACE_ARENA_NO_GIT') != '1':
        proc = subprocess.run(
            ('git', 'ls-files', '--cached', '--others', '--exclude-standard'),
            cwd=ROOT, capture_output=True, text=True, check=True)
        return tuple(sorted(p for p in proc.stdout.splitlines() if p))
    found = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        parts = Path(dirpath).relative_to(ROOT).parts
        dirnames.sort()
        dirnames[:] = [d for d in dirnames
                       if d != '.git' and not _ignored((*parts, d), True)]
        for name in filenames:
            if not _ignored((*parts, name)):
                found.append('/'.join((*parts, name)))
    return tuple(sorted(found))


PUBLISHED = _published_files()


def _is_cache(rel):
    parts = rel.split('/')
    return (any(seg in CACHE_DIR_NAMES for seg in parts[:-1])
            or parts[-1] in CACHE_FILE_NAMES
            or parts[-1].endswith(CACHE_FILE_SUFFIXES))


def _is_secret_name(name):
    if name == '.env' or (name.startswith('.env.') and name not in ENV_ALLOWLIST):
        return True
    return any(fnmatch.fnmatchcase(name, glob) for glob in SECRET_NAME_GLOBS)


def _text_blobs():
    for rel in PUBLISHED:
        if rel.startswith('vendor/'):
            continue
        path = ROOT / rel
        try:
            if path.stat().st_size > MAX_SCAN_BYTES:
                continue
            blob = path.read_bytes()
        except OSError:
            continue
        if b'\0' in blob[:4096]:
            continue
        yield rel, blob


class TestPublicationSet(unittest.TestCase):
    def test_working_tree_audit_does_not_invoke_git(self):
        with mock.patch.dict(os.environ, {'SPACE_ARENA_NO_GIT': '1'}), \
                mock.patch.object(subprocess, 'run',
                                  side_effect=AssertionError('Git is disabled')):
            files = _published_files()
        self.assertIn('README.md', files)
        self.assertTrue(all('.git' not in Path(rel).parts for rel in files))

    def test_publication_set_represents_real_sources(self):
        for expected in ('README.md', 'server/space/compose.json',
                         'tools/test_space_server.py'):
            self.assertIn(expected, PUBLISHED)

    def test_personal_journals_stay_out_of_publication(self):
        leaked = sorted(n for n in PUBLISHED
                        if '/' not in n and n in JOURNAL_NAMES)
        self.assertEqual(leaked, [])
        for name in sorted(JOURNAL_NAMES):
            self.assertTrue(_ignored((name,)),
                            name + ' lacks a .gitignore guard')

    def test_caches_and_virtualenvs_stay_out_of_publication(self):
        self.assertEqual(sorted(n for n in PUBLISHED if _is_cache(n)), [])

    def test_secret_named_files_stay_out_of_publication(self):
        offenders = sorted(n for n in PUBLISHED
                           if _is_secret_name(n.split('/')[-1]))
        self.assertEqual(offenders, [])

    def test_no_nested_git_directories(self):
        offenders = []
        for dirpath, dirnames, _ in os.walk(ROOT):
            rel = Path(dirpath).relative_to(ROOT)
            if '.git' in dirnames and rel.parts:
                if not _ignored((*rel.parts, '.git'), True):
                    offenders.append((rel / '.git').as_posix())
            dirnames.sort()
            dirnames[:] = [d for d in dirnames
                           if d != '.git'
                           and not _ignored((*rel.parts, d), True)]
        self.assertEqual(sorted(offenders), [])


class TestSourceContent(unittest.TestCase):
    def test_no_legacy_host_identifiers_outside_vendor(self):
        hits = sorted({rel for rel, blob in _text_blobs()
                       if any(token.encode() in blob.lower()
                              for token in LEGACY_TOKENS)})
        self.assertEqual(hits, [])

    def test_no_personal_absolute_paths_outside_vendor(self):
        hits = []
        for rel, blob in _text_blobs():
            text = blob.decode('utf-8', 'replace')
            if any(rx.search(text) for rx in PRIVATE_PATH_PATTERNS):
                hits.append(rel)
        self.assertEqual(sorted(hits), [])

    def test_patterns_do_not_match_this_test_file(self):
        blob = Path(__file__).read_text(encoding='utf-8')
        for token in LEGACY_TOKENS:
            self.assertNotIn(token, blob.lower())
        for rx in PRIVATE_PATH_PATTERNS:
            self.assertIsNone(rx.search(blob))


if __name__ == '__main__':
    unittest.main()
