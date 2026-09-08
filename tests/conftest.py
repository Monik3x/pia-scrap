import json
from pathlib import Path

import pytest


FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def novel_data():
    return {
        "result": {
            "novel": {
                "novel_no": 42,
                "novel_name": "A Test / Novel",
                "novel_story": "  A short description.  ",
                "flag_complete": 1,
                "count_epi": 2,
                "flag_detail_trans": 4,
            },
            "info": {"epi_cnt": 2},
            "writer_list": [{"writer_name": "Test Author"}],
            "tag_list": ["fantasy", {"tag_name": "adventure"}, "fantasy"],
        }
    }


@pytest.fixture
def episodes():
    return [
        {
            "episode_no": 101,
            "epi_num": 1,
            "epi_title": "First / Chapter",
            "flag_detail_trans": 2,
        },
        {
            "episode_no": 102,
            "epi_num": 2,
            "epi_title": "Second Chapter",
            "flag_detail_trans": 4,
        },
    ]


@pytest.fixture(scope="session")
def captured_api_samples():
    fixture_path = FIXTURES_DIR / "novelpia_api_samples.json"
    return json.loads(fixture_path.read_text(encoding="utf-8"))
