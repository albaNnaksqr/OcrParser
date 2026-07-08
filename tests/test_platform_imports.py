def test_platform_packages_import():
    import ocr_platform
    import ocr_platform.agent
    import ocr_platform.control

    assert ocr_platform is not None
    assert ocr_platform.agent is not None
    assert ocr_platform.control is not None
