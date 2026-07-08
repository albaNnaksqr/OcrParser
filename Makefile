.PHONY: compile test verify

PYTHON ?= python

compile:
	$(PYTHON) -m compileall -q ocr_parser dots_ocr services

test:
	$(PYTHON) -m pytest -q

verify: compile test
