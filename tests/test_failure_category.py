from ocr_parser.infra.failure_category import infer_failure_category as parser_infer_failure_category
from ocr_platform.control.service import infer_failure_category as control_infer_failure_category


def test_control_and_parser_use_same_failure_category_classifier():
    assert control_infer_failure_category is parser_infer_failure_category


def test_failure_category_classifies_positive_return_code_as_process_failed():
    payload = {"return_code": 2}

    assert control_infer_failure_category(payload) == "process_failed"
    assert parser_infer_failure_category(payload) == "process_failed"


def test_failure_category_classifies_killed_process_without_return_code():
    payload = {"error": "worker subprocess was killed by signal SIGKILL"}

    assert control_infer_failure_category(payload) == "process_killed"
    assert parser_infer_failure_category(payload) == "process_killed"


def test_failure_category_classifies_shell_sigkill_exit_code_as_process_killed():
    payload = {"return_code": 137}

    assert control_infer_failure_category(payload) == "process_killed"
    assert parser_infer_failure_category(payload) == "process_killed"


def test_failure_category_classifies_cuda_out_of_memory():
    payload = {"error": "CUDA out of memory. Tried to allocate 2.00 GiB"}

    assert control_infer_failure_category(payload) == "resource_exhausted"
    assert parser_infer_failure_category(payload) == "resource_exhausted"


def test_failure_category_classifies_model_auth_failures():
    cases = [
        {"error": "HTTPStatusError: 401 Unauthorized from model server"},
        {"error": "Model server returned 403 forbidden: invalid API key"},
        {"error": "authentication failed while calling model endpoint"},
    ]

    for payload in cases:
        assert control_infer_failure_category(payload) == "model_auth_failed"
        assert parser_infer_failure_category(payload) == "model_auth_failed"


def test_failure_category_classifies_model_rate_limits():
    cases = [
        {"error": "HTTPStatusError: 429 Too Many Requests from model server"},
        {"error": "model server rate limit exceeded"},
        {"error": "request throttled by upstream model endpoint"},
    ]

    for payload in cases:
        assert control_infer_failure_category(payload) == "model_rate_limited"
        assert parser_infer_failure_category(payload) == "model_rate_limited"


def test_failure_category_classifies_model_service_unavailable():
    cases = [
        {"error": "HTTPStatusError: 503 Service Unavailable from model server"},
        {"error": "502 Bad Gateway from upstream model endpoint"},
        {"error": "model server overloaded: temporarily unavailable"},
    ]

    for payload in cases:
        assert control_infer_failure_category(payload) == "model_unavailable"
        assert parser_infer_failure_category(payload) == "model_unavailable"


def test_failure_category_classifies_model_json_decode_failures():
    cases = [
        {"error": "JSONDecodeError: Expecting value: line 1 column 1 (char 0) while parsing model response"},
        {"error": "Expecting value: line 1 column 1 (char 0) from model server response"},
        {"error": "invalid JSON from model server: Expecting value"},
    ]

    for payload in cases:
        assert control_infer_failure_category(payload) == "model_output_invalid"
        assert parser_infer_failure_category(payload) == "model_output_invalid"


def test_failure_category_classifies_model_tls_connection_failures():
    cases = [
        {"error": "SSL certificate verify failed while connecting to model endpoint"},
        {"error": "SSLError: TLS handshake failed for model server"},
    ]

    for payload in cases:
        assert control_infer_failure_category(payload) == "model_unreachable"
        assert parser_infer_failure_category(payload) == "model_unreachable"


def test_failure_category_classifies_permission_denied_input_as_input_invalid():
    payload = {"error": "PermissionError: [Errno 13] Permission denied: '/shared/input/bad.pdf'"}

    assert control_infer_failure_category(payload) == "input_invalid"
    assert parser_infer_failure_category(payload) == "input_invalid"


def test_failure_category_classifies_missing_pdf_path_as_input_missing():
    payload = {"error": "FileNotFoundError: [Errno 2] No such file or directory: '/shared/docs/missing.pdf'"}

    assert control_infer_failure_category(payload) == "input_missing"
    assert parser_infer_failure_category(payload) == "input_missing"


def test_failure_category_classifies_corrupt_pdf_as_input_invalid():
    payload = {"error": "pymupdf.FileDataError: cannot open broken document /shared/input/bad.pdf"}

    assert control_infer_failure_category(payload) == "input_invalid"
    assert parser_infer_failure_category(payload) == "input_invalid"


def test_failure_category_classifies_password_protected_pdf_as_input_invalid():
    payload = {"error": "document requires a password before page rendering can continue"}

    assert control_infer_failure_category(payload) == "input_invalid"
    assert parser_infer_failure_category(payload) == "input_invalid"


def test_failure_category_classifies_output_path_shape_errors_as_output_unwritable():
    cases = [
        {"error": "FileNotFoundError: [Errno 2] No such file or directory: '/shared/out/a/a.md'"},
        {"error": "IsADirectoryError: [Errno 21] Is a directory: '/shared/output/a.md'"},
        {"error": "NotADirectoryError: [Errno 20] Not a directory: '/shared/artifacts/a.md'"},
    ]

    for payload in cases:
        assert control_infer_failure_category(payload) == "output_unwritable"
        assert parser_infer_failure_category(payload) == "output_unwritable"


def test_failure_category_classifies_unsupported_file_extension_as_input_invalid():
    payload = {"error": "File extension .txt not supported by the modular parser yet. Only .pdf is supported."}

    assert control_infer_failure_category(payload) == "input_invalid"
    assert parser_infer_failure_category(payload) == "input_invalid"


def test_failure_category_classifies_unrecognized_nonempty_error_as_parser_failed():
    payload = {"error": "unexpected exception while parsing page 7"}

    assert control_infer_failure_category(payload) == "parser_failed"
    assert parser_infer_failure_category(payload) == "parser_failed"
