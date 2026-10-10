from carbon_lens.config import ConfigStore
from pathlib import Path


def test_public_connection_list_hides_credentials():
    store = ConfigStore("unused.json")
    store._payload = {
        "connections": {
            "prod": {
                "ssh_config": {
                    "hostname": "carbon.example",
                    "username": "analyst",
                    "password": "do-not-return",
                }
            }
        }
    }
    listed = store.public_list()
    assert listed == [
        {
            "name": "prod",
            "transport": "beeline",
            "connection_mode": "ssh",
            "host": "carbon.example",
            "beeline_connection": "Carbon_Query_XDR",
        }
    ]
    assert "do-not-return" not in str(listed)


def test_local_ssh_paths_are_relative_to_config_directory():
    store = ConfigStore("D:/carbon-lens/config/carbon_lens.json")
    store._payload = {
        "connections": {
            "prod": {
                "ssh_config": {
                    "hostname": "carbon.example",
                    "username": "analyst",
                    "private_key": "keys/carbon_ed25519",
                    "known_hosts": "keys/known_hosts",
                }
            }
        }
    }
    config = store.get("prod")
    assert config.ssh["private_key"] == str(Path("D:/carbon-lens/config/keys/carbon_ed25519").resolve())
    assert config.known_hosts == str(Path("D:/carbon-lens/config/keys/known_hosts").resolve())
