"""Config is written atomically, and read-only reads stay reads.

The server is threaded — request handlers, job threads and the warm-up thread
all touch this module — so a save that used one fixed `config.json.tmp` name
let two threads collide and surfaced as a spurious FileNotFoundError during a
provision. These pin the two things that fix it: a unique temp name, and a
load() that does not write when it has nothing to change.
"""

import json
import os
import threading

from devdash import config


def test_load_creates_a_default_config_on_first_run(tmp_path):
    cfg = config.load(home=str(tmp_path))
    assert cfg == {"host": "", "port": config.DEFAULT_PORT, "web_dir": "web/dist"}
    assert os.path.exists(config.config_path(str(tmp_path)))


def test_load_leaves_a_config_it_did_not_need_to_change_alone(tmp_path):
    config.load(home=str(tmp_path))
    path = config.config_path(str(tmp_path))
    os.utime(path, (1, 1))  # a very old mtime a rewrite would clobber
    before = os.stat(path).st_mtime

    config.load(home=str(tmp_path))

    assert os.stat(path).st_mtime == before


def test_an_old_token_is_dropped_from_the_file(tmp_path):
    path = config.config_path(str(tmp_path))
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"host": "", "port": 3991, "token": "legacy"}, f)

    cfg = config.load(home=str(tmp_path))

    assert "token" not in cfg
    with open(path, encoding="utf-8") as f:
        assert "token" not in json.load(f)


def test_saving_is_written_with_owner_only_permissions(tmp_path):
    config.save({"host": "", "port": 3990}, home=str(tmp_path))
    mode = os.stat(config.config_path(str(tmp_path))).st_mode & 0o777
    assert mode == 0o600


def test_concurrent_loads_never_collide_on_the_temp_file(tmp_path):
    """The bug's actual shape: many threads, one home, no exceptions and no
    leftover temp files."""
    errors = []

    def worker():
        for _ in range(200):
            try:
                config.load(home=str(tmp_path))
            except Exception as e:  # noqa: BLE001 — the point is to catch anything
                errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert sorted(os.listdir(config.config_dir(str(tmp_path)))) == ["config.json"]
