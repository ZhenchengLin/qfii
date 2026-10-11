import pytest

from qfii.config import load_config


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "test.toml"
    path.write_text(f'[paths]\ndb = "{tmp_path / "qfii.db"}"\n'
                    f'[data]\ncsv_dir = "{tmp_path / "prices"}"\n')
    return load_config(path)


AS_OF = "2026-10-09"
