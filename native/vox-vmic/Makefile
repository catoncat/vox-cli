.PHONY: build-helper build-driver check clean

build-helper:
	swift build

build-driver:
	$(MAKE) -C driver

check: build-helper build-driver
	bash scripts/smoke-test.sh

clean:
	rm -rf .build
	$(MAKE) -C driver clean
