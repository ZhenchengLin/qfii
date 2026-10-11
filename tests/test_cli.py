from qfii import cli


def test_init_and_config(tmp_path, capsys):
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[paths]\ndb = "{tmp_path / "x.db"}"\n')
    assert cli.main(["--config", str(cfg), "init"]) == 0
    out = capsys.readouterr().out
    assert "initialised (schema v2)" in out and "$100,000.00" in out
    assert cli.main(["--config", str(cfg), "init"]) == 0
    assert "already initialised" in capsys.readouterr().out
    assert cli.main(["--config", str(cfg), "config"]) == 0
    out = capsys.readouterr().out
    assert "agent neutral" in out and "committee:  aggressive, conservative, neutral (equal votes)" in out


def test_bad_config_exit_code(tmp_path, capsys):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[execution]\nmode = "live"\n')
    assert cli.main(["--config", str(cfg), "config"]) == 2
    assert "config error" in capsys.readouterr().err
