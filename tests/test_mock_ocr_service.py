import json

from tools import mock_ocr_service


def test_mock_ocr_service_returns_valid_dotsocr_layout_text():
    content = mock_ocr_service.build_layout_response("walkthrough text")
    cells = json.loads(content)

    assert cells == [
        {
            "category": "Text",
            "bbox": [50, 50, 950, 180],
            "text": "walkthrough text",
        }
    ]


def test_mock_ocr_service_returns_openai_compatible_chat_completion_shape():
    payload = mock_ocr_service.build_chat_completion_response(
        model="mock-ocr",
        content=mock_ocr_service.build_layout_response("done"),
    )

    assert payload["object"] == "chat.completion"
    assert payload["model"] == "mock-ocr"
    assert payload["choices"][0]["message"]["role"] == "assistant"
    assert json.loads(payload["choices"][0]["message"]["content"])[0]["text"] == "done"
    assert payload["usage"]["total_tokens"] >= 1


def test_mock_ocr_service_accepts_response_delay():
    parser = mock_ocr_service.build_parser()

    assert parser.parse_args(["--response-delay", "0.25"]).response_delay == 0.25
