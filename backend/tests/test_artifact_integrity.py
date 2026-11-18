import pytest
from backend.config.settings import settings
from backend.services import artifact_builder as artifacts


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'SQLITE_DB_PATH', str(tmp_path / 'data.db'))
    monkeypatch.setattr(settings, 'STORAGE_BACKEND', 'sqlite')


def test_agent_files_are_owned_loadable_and_verified():
    saved = artifacts.save_user_files('alice', 'Report', [{'path': 'report.md', 'content': 'Observed result'}])
    aid = saved['artifact_id']
    assert artifacts.load_artifact('alice', aid)['user_id'] == 'alice'
    assert artifacts.read_artifact_content('alice', aid, 'report.md') == 'Observed result'
    with pytest.raises(artifacts.ArtifactError):
        artifacts.load_artifact('bob', aid)
    path = artifacts.read_artifact_file('alice', aid, 'report.md')
    path.write_text('tampered')
    with pytest.raises(artifacts.ArtifactError, match='integrity'):
        artifacts.read_artifact_file('alice', aid, 'report.md')


@pytest.mark.parametrize('path', ['../escape.md', 'artifact.json'])
def test_reserved_or_traversal_paths_rejected(path):
    with pytest.raises(artifacts.ArtifactError):
        artifacts.save_user_files('alice', 'Report', [{'path': path, 'content': 'x'}])


def test_manifest_required_for_file_access():
    saved = artifacts.save_user_files('alice', 'Report', [{'path': 'report.md', 'content': 'x'}])
    folder = artifacts._artifact_dir('alice', saved['artifact_id'])
    (folder / 'hidden.txt').write_text('not in manifest')
    with pytest.raises(artifacts.ArtifactError, match='not part'):
        artifacts.read_artifact_content('alice', saved['artifact_id'], 'hidden.txt')


def test_firestore_upload_uses_one_batch_and_accepts_dict_file_entries(monkeypatch):
    from unittest.mock import MagicMock
    db = MagicMock()
    monkeypatch.setattr(settings, 'STORAGE_BACKEND', 'firestore')
    monkeypatch.setattr(artifacts, '_firestore_sync', lambda: db)
    saved = artifacts.save_user_files('alice', 'Report', [{'path': 'report.md', 'content': 'x'}])
    assert saved['user_id'] == 'alice'
    assert db.batch.return_value.set.call_count == 2
    db.batch.return_value.commit.assert_called_once()
