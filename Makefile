.PHONY: help ledger-compile build docker-build docker-run clean

help:
	@echo "pocketful-settlement Makefile"
	@echo "make ledger-compile    Compile C ledger"
	@echo "make docker-build      Build Docker image"
	@echo "make docker-run        Run Docker container"
	@echo "make build             Full build"
	@echo "make clean             Clean artifacts"

ledger-compile:
	@cd ledger && gcc -fPIC -pthread -c ledger.c -o ledger.o && gcc -shared -pthread ledger.o -o ledger.so
	@echo "✓ ledger.so compiled"

docker-build:
	@docker build -t pocketful:stage1 stage-1/

docker-run: docker-build
	@docker run -p 8000:8000 pocketful:stage1

build: ledger-compile
	@pip install -r requirements.txt
	@echo "✓ Build complete"

clean:
	@rm -f ledger/*.o ledger/*.so
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@echo "✓ Cleaned"
