import pytest

from electrumx.lib.tx import Deserializer, TxStream
from electrumx.lib.hash import sha256
import random


tests = [
    "020000000192809f0b234cb850d71d020e678e93f074648ed0df5affd0c46d3bcb177f"
    "9ccf020000008b483045022100c5403bcf86c3ae7b8fd4ca0d1e4df6729cc1af05ff95"
    "d9726b43a64b41dd5d9902207fab615f41871885aa3062fc7d8f8d9d3dcbc2e4867c5d"
    "96dd7a176b99e927924141040baa4271a82c5f1a09a5ea63d763697ca0545b6049c4dd"
    "8e8d099dd91f2da10eb11e829000a82047ac56969fb582433067a21c3171e569d1832c"
    "34fdd793cfc8ffffffff030000000000000000226a20195ce612d20e5284eb78bb28c9"
    "c50d6139b10b77b2d5b2f94711b13162700472bfc53000000000001976a9144a519c63"
    "f985ba5ab8b71bb42f1ecb82a0a0d80788acf6984315000000001976a9148b80536aa3"
    "c460258cda834b86a46787c9a2b0bf88ac00000000",
    "0200000003ee933f16c93d433bfd45426e94ddb4c0406aecac66f1c23a9e55151430c2"
    "b4c4a70000006b483045022100c940624ddbefcf86f3a3d820347de9c695204583a3ce"
    "9977c45cb1d6f69d577202205e0d9e9c0bfc2993160edad71cd1474b6308d990a89a54"
    "c80d7c62295edd2a394121039999e7c52a1447916b2af30478669c4e6e3ce52b895da7"
    "78f0b46b73f8ea6f00feffffff30be91ac83fee19e06304732027a39f9ec9018c5aae1"
    "159bf1c5de53dea61785fc0200006a473044022057cee17c3e123864336bdd6373e4bc"
    "0bf030ca70dd078520705930a4168610d0022061fdeedfbeb0b89c3b20db220799978b"
    "c94e11e4155b810b8b59df019b3f1859412103e2074e4387eeb3b1dec2fc8b953b0c13"
    "c53d0d3b7b676e1b3ca75ee606ecf750feffffffdd462d60e51be69451f06c36ba4490"
    "aaad8ebf40501dd27341e9c64a5f633324000000006a473044022043511ab874f0037c"
    "5726b1efc69fcdb638fac74ab3f6766eb80947cff8c1175a02200606ccf8db60f56e77"
    "03f6d5b81f5f5141f5b029a7b5a35700907f368b7e0f024121038daab4c77b9a428efb"
    "23aa2ccadc5c5332f299f5e51d1f1600524c0313ab9ec2feffffff04fe66c200000000"
    "001976a914a8c27c62fafec0a07d28b3b905912e9f385a7f1a88ac2a3b560800000000"
    "1976a914c20992e92764ef7e33e2cf6ed538d34b18a1fff888ac00a3e1110000000019"
    "76a9147137cd9dc7aad0d1cbc8e0ec12aae753d1acfaf488ac5d2b0f00000000001976"
    "a914c10084f449e968b0d71ee23a308954c68d8c97e488ac28620700"
]

def test_tx_serialiazation():
    for test in tests:
        test = bytes.fromhex(test)
        deser = Deserializer(test)
        tx = deser.read_tx()
        assert tx.serialize() == test


class EndOfStream(Exception):
    pass


class StreamedData:

    def __init__(self, data):
        self.data = data
        self.cursor = 0

    async def fetch_next(self):
        remaining = len(self.data) - self.cursor
        if remaining == 0:
            raise EndOfStream
        size = random.randrange(0, remaining) + 1
        cursor = self.cursor
        self.cursor += size
        return self.data[cursor: self.cursor]


class TestTxStream:

    @pytest.mark.asyncio
    async def test_simple(self):
        data = bytes(range(64))
        sdata = StreamedData(data)
        stream = TxStream(sdata.fetch_next)
        expected_hash = sha256(data)
        stream_data = await stream.read(len(data))
        stream_hash = stream.get_hash()
        assert stream_data.hex() == data.hex()
        assert stream_hash.hex() == expected_hash.hex()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw_tx_hex", tests)
    async def test_read_tx(self, raw_tx_hex):
        raw_tx = bytes.fromhex(raw_tx_hex)
        sdata = StreamedData(raw_tx)
        stream = TxStream(sdata.fetch_next)
        expected_tx_hash = sha256(raw_tx)
        tx, tx_hash = await stream.read_tx()
        assert tx.serialize().hex() == raw_tx_hex
        assert tx_hash == expected_tx_hash
