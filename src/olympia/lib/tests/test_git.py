import os
import subprocess
import zipfile

import pytest
import pygit2
import mock

from django.core.files import temp
from django.core.files.base import File as DjangoFile

from olympia import amo
from olympia.amo.tests import (
    addon_factory, version_factory, user_factory, activate_locale)
from olympia.lib.git import AddonGitRepository, TemporaryWorktree
from olympia.files.utils import id_to_path


def _run_process(cmd, repo):
    """Small helper to run git commands on the shell"""
    return subprocess.check_output(
        cmd,
        shell=True,
        env={'GIT_DIR': repo.git_repository.path},
        universal_newlines=True)


def test_temporary_worktree(settings):
    repo = AddonGitRepository(1)

    output = _run_process('git worktree list', repo)
    assert output.startswith(repo.git_repository.path)

    with TemporaryWorktree(repo.git_repository) as worktree:
        assert worktree.temp_directory.startswith(settings.TMP_PATH)
        assert worktree.path == os.path.join(
            worktree.temp_directory, worktree.name)

        output = _run_process('git worktree list', repo)
        assert worktree.name in output

    # Test that it cleans up properly
    assert not os.path.exists(worktree.temp_directory)
    output = _run_process('git worktree list', repo)
    assert worktree.name not in output


def test_enforce_pygit_global_search_path(settings):
    pygit2.settings.search_path[pygit2.GIT_CONFIG_LEVEL_GLOBAL] = '/root'

    assert (
        pygit2.settings.search_path[pygit2.GIT_CONFIG_LEVEL_GLOBAL] ==
        '/root')

    # Now initialize, which will overwrite the global setting.
    AddonGitRepository(1)

    assert (
        pygit2.settings.search_path[pygit2.GIT_CONFIG_LEVEL_GLOBAL] ==
        settings.ROOT)


def test_git_repo_init(settings):
    repo = AddonGitRepository(1)

    assert repo.git_repository_path == os.path.join(
        settings.GIT_FILE_STORAGE_PATH, '1/1/1', 'addon')

    assert not os.path.exists(repo.git_repository_path)

    # accessing repo.git_repository creates the directory
    assert sorted(os.listdir(repo.git_repository.path)) == sorted([
        'objects', 'refs', 'hooks', 'info', 'description', 'config',
        'HEAD', 'logs'])


def test_git_repo_init_opens_existing_repo(settings):
    expected_path = os.path.join(
        settings.GIT_FILE_STORAGE_PATH, '1/1/1', 'addon')

    assert not os.path.exists(expected_path)
    repo = AddonGitRepository(1)
    assert not os.path.exists(expected_path)

    # accessing repo.git_repository creates the directory
    repo.git_repository
    assert os.path.exists(expected_path)

    repo2 = AddonGitRepository(1)
    assert repo.git_repository.path == repo2.git_repository.path


@pytest.mark.django_db
def test_extract_and_commit_from_version(settings):
    addon = addon_factory(file_kw={'filename': 'webextension_no_id.xpi'})

    repo = AddonGitRepository.extract_and_commit_from_version(
        addon.current_version)

    assert repo.git_repository_path == os.path.join(
        settings.GIT_FILE_STORAGE_PATH, id_to_path(addon.id), 'addon')
    assert os.listdir(repo.git_repository_path) == ['.git']

    # Verify via subprocess to make sure the repositories are properly
    # read by the regular git client
    output = _run_process('git branch', repo)
    assert 'listed' in output
    assert 'unlisted' not in output

    # Test that a new "unlisted" branch is created only if needed
    addon.current_version.update(channel=amo.RELEASE_CHANNEL_UNLISTED)
    repo = AddonGitRepository.extract_and_commit_from_version(
        version=addon.current_version)
    output = _run_process('git branch', repo)
    assert 'listed' in output
    assert 'unlisted' in output

    output = _run_process('git log listed', repo)
    expected = 'Create new version {} ({}) for {} from {}'.format(
        repr(addon.current_version), addon.current_version.id, repr(addon),
        repr(addon.current_version.all_files[0]))
    assert expected in output


@pytest.mark.django_db
def test_extract_and_commit_from_version_set_git_hash():
    addon = addon_factory(file_kw={'filename': 'webextension_no_id.xpi'})

    assert addon.current_version.git_hash == ''

    AddonGitRepository.extract_and_commit_from_version(
        version=addon.current_version)

    addon.current_version.refresh_from_db()
    assert len(addon.current_version.git_hash) == 40


@pytest.mark.django_db
def test_extract_and_commit_from_version_multiple_versions(settings):
    addon = addon_factory(
        file_kw={'filename': 'webextension_no_id.xpi'},
        version_kw={'version': '0.1'})

    repo = AddonGitRepository.extract_and_commit_from_version(
        addon.current_version)

    assert repo.git_repository_path == os.path.join(
        settings.GIT_FILE_STORAGE_PATH, id_to_path(addon.id), 'addon')
    assert os.listdir(repo.git_repository_path) == ['.git']

    # Verify via subprocess to make sure the repositories are properly
    # read by the regular git client
    output = _run_process('git branch', repo)
    assert 'listed' in output

    output = _run_process('git log listed', repo)
    expected = 'Create new version {} ({}) for {} from {}'.format(
        repr(addon.current_version), addon.current_version.id, repr(addon),
        repr(addon.current_version.all_files[0]))
    assert expected in output

    # Create two more versions, check that they appear in the comitlog
    version = version_factory(
        addon=addon, file_kw={'filename': 'webextension_no_id.xpi'},
        version='0.2')
    AddonGitRepository.extract_and_commit_from_version(version=version)

    version = version_factory(
        addon=addon, file_kw={'filename': 'webextension_no_id.xpi'},
        version='0.3')
    repo = AddonGitRepository.extract_and_commit_from_version(version=version)

    output = _run_process('git log listed', repo)
    assert output.count('Create new version') == 3
    assert '0.1' in output
    assert '0.2' in output
    assert '0.3' in output

    # 4 actual commits, including the repo initialization
    assert output.count('Mozilla Add-ons Robot') == 4

    # Make sure the commits didn't spill over into the master branch
    output = _run_process('git log', repo)
    assert output.count('Mozilla Add-ons Robot') == 1
    assert '0.1' not in output


@pytest.mark.django_db
def test_extract_and_commit_from_version_use_applied_author():
    addon = addon_factory(file_kw={'filename': 'webextension_no_id.xpi'})
    user = user_factory(
        email='fancyuser@foo.bar', display_name='Fancy Test User')

    repo = AddonGitRepository.extract_and_commit_from_version(
        version=addon.current_version,
        author=user)

    output = _run_process('git log --format=full listed', repo)
    assert 'Author: Fancy Test User <fancyuser@foo.bar>' in output
    assert (
        'Commit: Mozilla Add-ons Robot '
        '<addons-dev-automation+github@mozilla.com>'
        in output)


@pytest.mark.django_db
def test_extract_and_commit_from_version_use_addons_robot_default():
    addon = addon_factory(file_kw={'filename': 'webextension_no_id.xpi'})
    repo = AddonGitRepository.extract_and_commit_from_version(
        version=addon.current_version)

    output = _run_process('git log --format=full listed', repo)
    assert (
        'Author: Mozilla Add-ons Robot '
        '<addons-dev-automation+github@mozilla.com>'
        in output)
    assert (
        'Commit: Mozilla Add-ons Robot '
        '<addons-dev-automation+github@mozilla.com>'
        in output)


@pytest.mark.django_db
@pytest.mark.parametrize('filename', [
    'webextension_no_id.xpi',
    'webextension_no_id.zip',
    'search.xml',
])
def test_extract_and_commit_from_version_valid_extensions(settings, filename):
    addon = addon_factory(file_kw={'filename': filename})

    with mock.patch('olympia.files.utils.os.fsync') as fsync_mock:
        repo = AddonGitRepository.extract_and_commit_from_version(
            addon.current_version)

        # Make sure we are always calling fsync after extraction
        assert fsync_mock.called

    assert repo.git_repository_path == os.path.join(
        settings.GIT_FILE_STORAGE_PATH, id_to_path(addon.id), 'addon')
    assert os.listdir(repo.git_repository_path) == ['.git']

    # Verify via subprocess to make sure the repositories are properly
    # read by the regular git client
    output = _run_process('git branch', repo)
    assert 'listed' in output
    assert 'unlisted' not in output

    output = _run_process('git log listed', repo)
    expected = 'Create new version {} ({}) for {} from {}'.format(
        repr(addon.current_version), addon.current_version.id, repr(addon),
        repr(addon.current_version.all_files[0]))
    assert expected in output


@pytest.mark.django_db
def test_extract_and_commit_source_from_version(settings):
    addon = addon_factory(
        file_kw={'filename': 'webextension_no_id.xpi'},
        version_kw={'version': '0.1'})

    # Generate source file
    source = temp.NamedTemporaryFile(suffix='.zip', dir=settings.TMP_PATH)
    with zipfile.ZipFile(source, 'w') as zip_file:
        zip_file.writestr('manifest.json', '{}')
    source.seek(0)
    addon.current_version.source = DjangoFile(source)
    addon.current_version.save()

    repo = AddonGitRepository.extract_and_commit_source_from_version(
        addon.current_version)

    assert repo.git_repository_path == os.path.join(
        settings.GIT_FILE_STORAGE_PATH, id_to_path(addon.id), 'source')
    assert os.listdir(repo.git_repository_path) == ['.git']

    # Verify via subprocess to make sure the repositories are properly
    # read by the regular git client
    output = _run_process('git branch', repo)
    assert 'listed' in output

    output = _run_process('git log listed', repo)
    expected = 'Create new version {} ({}) for {} from source file'.format(
        repr(addon.current_version), addon.current_version.id, repr(addon))
    assert expected in output


@pytest.mark.django_db
def test_extract_and_commit_source_from_version_no_dotgit_clash(settings):
    addon = addon_factory(
        file_kw={'filename': 'webextension_no_id.xpi'},
        version_kw={'version': '0.1'})

    # Generate source file
    source = temp.NamedTemporaryFile(suffix='.zip', dir=settings.TMP_PATH)
    with zipfile.ZipFile(source, 'w') as zip_file:
        zip_file.writestr('manifest.json', '{}')
        zip_file.writestr('.git/config', '')
    source.seek(0)
    addon.current_version.source = DjangoFile(source)
    addon.current_version.save()

    with mock.patch('olympia.lib.git.uuid.uuid4') as uuid4_mock:
        uuid4_mock.return_value = mock.Mock(
            hex='b236f5994773477bbcd2d1b75ab1458f')
        repo = AddonGitRepository.extract_and_commit_source_from_version(
            addon.current_version)

    assert repo.git_repository_path == os.path.join(
        settings.GIT_FILE_STORAGE_PATH, id_to_path(addon.id), 'source')
    assert os.listdir(repo.git_repository_path) == ['.git']

    # Verify via subprocess to make sure the repositories are properly
    # read by the regular git client
    output = _run_process('git ls-tree -r --name-only listed', repo)
    assert set(output.split()) == {
        'extracted/manifest.json', 'extracted/.git.b236f599/config'}


@pytest.mark.django_db
@pytest.mark.parametrize('filename, expected', [
    ('webextension_no_id.xpi', {'README.md', 'manifest.json'}),
    ('webextension_no_id.zip', {'README.md', 'manifest.json'}),
    ('search.xml', {'search.xml'}),
    ('notify-link-clicks-i18n.xpi', {
        'README.md', '_locales/de/messages.json', '_locales/en/messages.json',
        '_locales/ja/messages.json', '_locales/nb_NO/messages.json',
        '_locales/nl/messages.json', '_locales/ru/messages.json',
        '_locales/sv/messages.json', 'background-script.js',
        'content-script.js', 'icons/LICENSE', 'icons/link-48.png',
        'manifest.json'})
])
def test_extract_and_commit_from_version_commits_files(
        settings, filename, expected):
    addon = addon_factory(file_kw={'filename': filename})

    repo = AddonGitRepository.extract_and_commit_from_version(
        addon.current_version)

    # Verify via subprocess to make sure the repositories are properly
    # read by the regular git client
    output = _run_process(
        'git ls-tree -r --name-only listed:extracted', repo)

    assert set(output.split()) == expected


@pytest.mark.django_db
def test_extract_and_commit_from_version_reverts_active_locale():
    from django.utils.translation import get_language

    addon = addon_factory(
        file_kw={'filename': 'webextension_no_id.xpi'},
        version_kw={'version': '0.1'})

    with activate_locale('fr'):
        repo = AddonGitRepository.extract_and_commit_from_version(
            addon.current_version)
        assert get_language() == 'fr'

    output = _run_process('git log listed', repo)
    expected = 'Create new version {} ({}) for {} from {}'.format(
        repr(addon.current_version), addon.current_version.id, repr(addon),
        repr(addon.current_version.all_files[0]))
    assert expected in output


@pytest.mark.django_db
def test_iter_tree():
    addon = addon_factory(file_kw={'filename': 'notify-link-clicks-i18n.xpi'})

    repo = AddonGitRepository.extract_and_commit_from_version(
        addon.current_version)

    commit = repo.git_repository.revparse_single('listed')

    tree = list(repo.iter_tree(repo.get_root_tree(commit)))

    # path, filename mapping
    expected_files = [
        ('README.md', 'README.md', 'blob'),
        ('_locales', '_locales', 'tree'),
        ('_locales/de', 'de', 'tree'),
        ('_locales/de/messages.json', 'messages.json', 'blob'),
        ('_locales/en', 'en', 'tree'),
        ('_locales/en/messages.json', 'messages.json', 'blob'),
        ('_locales/ja', 'ja', 'tree'),
        ('_locales/ja/messages.json', 'messages.json', 'blob'),
        ('_locales/nb_NO', 'nb_NO', 'tree'),
        ('_locales/nb_NO/messages.json', 'messages.json', 'blob'),
        ('_locales/nl', 'nl', 'tree'),
        ('_locales/nl/messages.json', 'messages.json', 'blob'),
        ('_locales/ru', 'ru', 'tree'),
        ('_locales/ru/messages.json', 'messages.json', 'blob'),
        ('_locales/sv', 'sv', 'tree'),
        ('_locales/sv/messages.json', 'messages.json', 'blob'),
        ('background-script.js', 'background-script.js', 'blob'),
        ('content-script.js', 'content-script.js', 'blob'),
        ('icons', 'icons', 'tree'),
        ('icons/LICENSE', 'LICENSE', 'blob'),
        ('icons/link-48.png', 'link-48.png', 'blob'),
        ('manifest.json', 'manifest.json', 'blob'),
    ]

    for idx, entry in enumerate(tree):
        expected_path, expected_name, expected_type = expected_files[idx]
        assert entry.path == expected_path
        assert entry.tree_entry.name == expected_name
        assert entry.tree_entry.type == expected_type
