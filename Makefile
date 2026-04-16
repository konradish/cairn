.PHONY: build view clean help

PY ?= python3
HTML := index.html

help:
	@echo "cairn — triage feed for the orchestrator"
	@echo ""
	@echo "targets:"
	@echo "  make build    regenerate $(HTML) from live hive + git data"
	@echo "  make view     open $(HTML) in the default browser"
	@echo "  make clean    delete $(HTML)"

build:
	$(PY) build.py

view: build
	@if command -v wslview >/dev/null 2>&1; then \
		wslview $(HTML); \
	elif command -v explorer.exe >/dev/null 2>&1; then \
		explorer.exe $(HTML) || true; \
	elif command -v xdg-open >/dev/null 2>&1; then \
		xdg-open $(HTML); \
	elif command -v open >/dev/null 2>&1; then \
		open $(HTML); \
	else \
		echo "no opener found — open $(HTML) manually"; \
	fi

clean:
	rm -f $(HTML)
