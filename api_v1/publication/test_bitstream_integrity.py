import tempfile
import unittest
from pathlib import Path

from api_v1.publication.verify_baseline import verify_bitstream_structure


def bit_image(payload):
    header = b'\x00\x09' + bytes.fromhex('0ff00ff00ff00ff000') + b'\x00\x01'
    for tag, value in ((b'a', b'test\0'), (b'b', b'7z020clg400\0'),
                       (b'c', b'2026/09/28\0'), (b'd', b'12:00:00\0')):
        header += tag + len(value).to_bytes(2, 'big') + value
    return header + b'e' + len(payload).to_bytes(4, 'big') + payload


class BitstreamIntegrityTest(unittest.TestCase):
    def check_image(self, data):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'image.bit'
            path.write_bytes(data)
            return verify_bitstream_structure(path)

    def test_complete(self):
        result = self.check_image(bit_image(bytes.fromhex('ffffffffaa99556620000000')))
        self.assertTrue(result['ok'])

    def test_truncated_payload(self):
        data = bit_image(bytes.fromhex('ffffffffaa99556620000000'))
        result = self.check_image(data[:-3])
        self.assertFalse(result['ok'])
        self.assertIn('payload length mismatch', result['error'])

    def test_truncated_header(self):
        self.assertFalse(self.check_image(b'\x00\x09')['ok'])

    def test_missing_sync(self):
        self.assertFalse(self.check_image(bit_image(bytes(16)))['ok'])

    def test_extra_bytes(self):
        data = bit_image(bytes.fromhex('aa99556620000000'))
        self.assertFalse(self.check_image(data + b'\x00')['ok'])


if __name__ == '__main__':
    unittest.main()
