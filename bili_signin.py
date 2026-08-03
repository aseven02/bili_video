import time
import urllib.parse
from hashlib import md5
from typing import Any


class BilibiliSign:
    """Sign Bilibili WBI request params."""

    _MIXIN_KEY_ENC_TAB = [
        46,
        47,
        18,
        2,
        53,
        8,
        23,
        32,
        15,
        50,
        10,
        31,
        58,
        3,
        45,
        35,
        27,
        43,
        5,
        49,
        33,
        9,
        42,
        19,
        29,
        28,
        14,
        39,
        12,
        38,
        41,
        13,
        37,
        48,
        7,
        16,
        24,
        55,
        40,
        61,
        26,
        17,
        0,
        1,
        60,
        51,
        30,
        4,
        22,
        25,
        54,
        21,
        56,
        59,
        6,
        63,
        57,
        62,
        11,
        36,
        20,
        34,
        44,
        52,
    ]

    def __init__(self, img_key: str, sub_key: str) -> None:
        if not img_key or not sub_key:
            raise ValueError("img_key and sub_key are required")
        self._mixin_key = self._make_mixin_key(img_key + sub_key)

    @classmethod
    def _make_mixin_key(cls, key: str) -> str:
        if len(key) <= max(cls._MIXIN_KEY_ENC_TAB):
            raise ValueError("WBI key is too short")
        return "".join(key[index] for index in cls._MIXIN_KEY_ENC_TAB)[:32]

    def sign(self, params: dict[str, Any]) -> dict[str, str]:
        signed_params = {**params, "wts": int(time.time())}
        signed_params = dict(sorted(signed_params.items()))
        filtered_params = {
            key: "".join(ch for ch in str(value) if ch not in "!'()*")
            for key, value in signed_params.items()
        }
        query = urllib.parse.urlencode(filtered_params)
        filtered_params["w_rid"] = md5((query + self._mixin_key).encode()).hexdigest()
        return filtered_params
