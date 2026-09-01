import json
import unittest
from unittest.mock import patch

from hcs_ai import model_manager


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class ModelSearchSizesTests(unittest.TestCase):
    def test_sibling_size_accepts_lfs_metadata(self):
        self.assertEqual(model_manager._sibling_size({"lfs": {"size": 1234}}), 1234)

    @patch("hcs_ai.model_manager.urllib.request.urlopen")
    def test_search_enriches_missing_file_sizes(self, urlopen):
        urlopen.side_effect = [
            _Response([{
                "id": "example/Qwen3-GGUF",
                "downloads": 42,
                "siblings": [{"rfilename": "Qwen3-Q4_K_M.gguf"}],
            }]),
            _Response({
                "siblings": [{
                    "rfilename": "Qwen3-Q4_K_M.gguf",
                    "size": 5_368_709_120,
                }],
            }),
        ]

        rows = model_manager.search_huggingface("Qwen3 GGUF", limit=1)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["size_bytes"], 5_368_709_120)
        self.assertEqual(urlopen.call_count, 2)
        detail_url = urlopen.call_args_list[1].args[0].full_url
        self.assertIn("/api/models/example/Qwen3-GGUF?blobs=true", detail_url)

    @patch("hcs_ai.model_manager.urllib.request.urlopen")
    def test_search_keeps_results_when_metadata_lookup_fails(self, urlopen):
        urlopen.side_effect = [
            _Response([{
                "id": "example/model-GGUF",
                "siblings": [{"rfilename": "model-Q4.gguf"}],
            }]),
            OSError("metadata unavailable"),
        ]

        rows = model_manager.search_huggingface("model", limit=1)

        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["size_bytes"])


if __name__ == "__main__":
    unittest.main()
